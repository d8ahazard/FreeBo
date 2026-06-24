"""AudioSink — turns the robot's live decoded audio into utterances for the brain.

Subscribes to the MediaHub's audio (16 kHz mono PCM from `agora_native`), runs a cheap energy-based VAD to
slice out speech, and hands each finished utterance to a worker thread that transcribes it (faster-whisper,
lazy + optional) and calls `on_utterance(text)`. This is the native, server-side replacement for the old
browser-mediated mic path: the robot's built-in mic -> Agora -> here -> brain.feed_speech.

Like the other heavy consumers it never blocks the decode loop — chunks are appended cheaply and all STT
happens off-thread. RMS uses stdlib `audioop` so VAD itself adds no dependency.
"""
from __future__ import annotations

import audioop
import os
import re
import threading
import time
from collections import deque
from typing import Callable, Optional

# Phrases Whisper notoriously invents on silence/noise (subtitle/credit boilerplate). If a whole transcript
# is just one of these (or empty punctuation), it's almost certainly a hallucination — drop it.
_HALLUCINATIONS = {
    "you", "thank you", "thank you.", "thanks for watching", "thanks for watching!",
    "thank you for watching", "please subscribe", "bye", "bye.", "okay", "ok",
    "you're welcome", ".", "...", "[ silence ]", "[silence]", "[ music ]", "[music]",
}


def _is_hallucination(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    if t in _HALLUCINATIONS:
        return True
    # only punctuation / no letters at all
    if not re.search(r"[a-z0-9]", t):
        return True
    return False


class AudioSink:
    def __init__(self, on_utterance: Optional[Callable[[str], None]] = None, *,
                 on_critical: Optional[Callable[[str], None]] = None,
                 sample_rate: int = 16000, rms_threshold: int = 0,
                 min_speech_s: float = 0.0, hang_s: float = 0.0, max_utt_s: float = 0.0) -> None:
        # The Air 2 mic comes in quiet over G.711 (speech ~400-700 RMS), so the VAD threshold is low and
        # env-tunable; each utterance is gain-normalized before Whisper (below). All VAD timings are env
        # tunable so you can tighten/loosen segmentation without code changes.
        #
        # Adaptive noise floor (Phase 0.4): when neither an explicit `rms_threshold` arg nor `AUTOBOT_STT_RMS`
        # is set, the enter/exit thresholds track a rolling noise floor learned ONLY during confident silence
        # (and never while we're playing our own TTS — those chunks are dropped before they reach here). The
        # floor is clamped to [RMS_MIN, RMS_MAX] so it can never "learn" speech-level energy as background
        # noise. Separate enter/exit thresholds give hysteresis. A fixed `AUTOBOT_STT_RMS` (or an explicit arg)
        # disables adaptation entirely (rollback path) — enter == exit == that value, exactly as before.
        # Provisional defaults; calibrate from docs/AUDIO_DIAGNOSTIC.md (Phase 0.3).
        _fixed_env = os.environ.get("AUTOBOT_STT_RMS")
        self.adaptive = (rms_threshold <= 0) and (_fixed_env is None)
        self.rms_threshold = rms_threshold or int(_fixed_env or "900")   # fixed value / static fallback
        self.rms_min = int(os.environ.get("AUTOBOT_STT_RMS_MIN", "250"))
        self.rms_max = int(os.environ.get("AUTOBOT_STT_RMS_MAX", "1500"))
        self.enter_k = float(os.environ.get("AUTOBOT_STT_ENTER_K", "2.5"))
        self.exit_k = float(os.environ.get("AUTOBOT_STT_EXIT_K", "1.5"))
        self._floor = float(self.rms_min)        # EMA noise floor (adaptive mode only)
        self._floor_alpha = float(os.environ.get("AUTOBOT_STT_FLOOR_ALPHA", "0.05"))
        self.on_utterance = on_utterance
        self.sample_rate = sample_rate
        self.min_speech_s = min_speech_s or float(os.environ.get("AUTOBOT_STT_MIN_SPEECH", "0.4"))
        self.hang_s = hang_s or float(os.environ.get("AUTOBOT_STT_HANG", "0.7"))
        self.max_utt_s = max_utt_s or float(os.environ.get("AUTOBOT_STT_MAX_UTT", "12.0"))
        self.queue_max = int(os.environ.get("AUTOBOT_STT_QUEUE", "6"))

        self._buf = bytearray()
        self._in_speech = False
        self._last_voice_ts = 0.0
        self._speech_start = 0.0
        self._lock = threading.Lock()
        self._jobs: "deque[bytes]" = deque()
        self._cv = threading.Condition()
        self._running = False
        self._worker: Optional[threading.Thread] = None
        self._unsub_audio = None          # MediaHub unsubscribe handle (so stop() removes our subscriber)
        self._whisper = None
        self._stt_device_loaded = None     # the actual device:compute:model Whisper loaded on (or CPU fallback)
        # Normal STT and barge-in STT share ONE Faster-Whisper model; serialize access so they never run it
        # concurrently (CTranslate2 models are not safe to call from two threads at once).
        self._whisper_lock = threading.Lock()
        self.utterances = 0
        self.last_error: Optional[str] = None
        self._nchunks = 0
        self._max_rms = 0
        self._last_text = ""
        # Drop diagnostics (why a delivered chunk didn't reach the VAD) — to debug "not listening".
        self._recv = 0
        self._drop_empty = 0
        self._drop_speaking = 0
        self._drop_rms = 0
        # --- per-stage diagnostics (Phase 0.2; instrumentation only — does NOT alter VAD behavior) ---
        # Stage counters + last-timing values let the diagnostics run (GET /api/diag/audio) attribute a
        # listening failure to a specific stage (no audio / VAD never fires / STT slow / hallucination drop).
        self._vad_starts = 0          # times the VAD entered speech
        self._vad_ends = 0            # times an utterance segment closed (accepted or dropped)
        self._seg_accepted = 0        # segments long enough to enqueue for STT
        self._seg_dropped_short = 0   # segments dropped as too-short (cough/click)
        self._stt_runs = 0            # STT transcriptions attempted
        self._stt_fail = 0            # STT transcriptions that raised
        self._last_seg_dur = 0.0      # duration (s) of the last closed segment
        self._last_stt_ms = 0.0       # wall ms of the last transcription
        self._last_queue_wait_ms = 0.0  # ms a segment waited in the STT queue before processing
        self._events: "deque[dict]" = deque(maxlen=64)  # recent stage transitions (NOT per-packet)
        # --- window-scoped diagnostics (Correction 4): per-session RMS / STT / queue samples + transcripts so
        # a measurement window can report a real distribution (count/min/mean/p50/p90/p95/p99/max), not just a
        # cumulative max. diag_reset() starts an epoch; diag_window() reports since the reset.
        self._rms_samples: "deque[int]" = deque(maxlen=30000)
        self._stt_samples: "deque[float]" = deque(maxlen=1000)
        self._queue_samples: "deque[float]" = deque(maxlen=1000)
        self._diag_transcripts: "deque[str]" = deque(maxlen=200)
        self._diag_base: dict = {}
        self._diag_started = 0.0
        # --- barge-in (Phase 0.5): detect STOP/QUIET DURING our own TTS so the robot can be interrupted ---
        # The MediaHub audio callback only pushes the newest audio into a bounded single-window buffer and
        # returns (never STT/blocks). A dedicated worker classifies it (PCM -> STT -> narrow keyword match),
        # energy-gated + self-echo-rejected. on_critical fires the moment a keyword lands (clock starts at the
        # keyword, not the normal VAD hang). on_critical MUST do its own thread-safe loop handoff.
        self.on_critical = on_critical
        self._bargein = os.environ.get("AUTOBOT_BARGEIN", "1").strip().lower() in ("1", "true", "yes", "on")
        self._bi_min_rms = int(os.environ.get("AUTOBOT_BARGEIN_MIN_RMS", "1500"))
        self._bi_window_s = float(os.environ.get("AUTOBOT_BARGEIN_WINDOW", "1.2"))
        self._bi_min_interval = float(os.environ.get("AUTOBOT_BARGEIN_INTERVAL", "0.5"))
        self._bi_buf = bytearray()
        self._bi_lock = threading.Lock()
        self._bi_event = threading.Event()
        self._bi_thread: Optional[threading.Thread] = None
        self._bi_last_run = 0.0
        self.bargein_fires = 0

    def _ev(self, stage: str, **kw) -> None:
        """Append a lightweight stage-transition record (deque.append is atomic in CPython — no lock)."""
        try:
            self._events.append({"t": round(time.time(), 3), "stage": stage, **kw})
        except Exception:  # noqa: BLE001
            pass

    # --- window-scoped diagnostics ---
    @staticmethod
    def _pct(sorted_vals: list, q: float):
        if not sorted_vals:
            return None
        k = (len(sorted_vals) - 1) * q
        f = int(k)
        c = min(f + 1, len(sorted_vals) - 1)
        if f == c:
            return round(float(sorted_vals[f]), 1)
        return round(float(sorted_vals[f]) + (sorted_vals[c] - sorted_vals[f]) * (k - f), 1)

    def _stats(self, vals: list) -> dict:
        if not vals:
            return {"count": 0}
        s = sorted(vals)
        return {"count": len(s), "min": round(float(s[0]), 1), "mean": round(sum(s) / len(s), 1),
                "p50": self._pct(s, 0.5), "p90": self._pct(s, 0.9), "p95": self._pct(s, 0.95),
                "p99": self._pct(s, 0.99), "max": round(float(s[-1]), 1)}

    def diag_reset(self) -> None:
        """Start a fresh measurement epoch (e.g. before an idle window, then again before a speech window)."""
        self._rms_samples.clear()
        self._stt_samples.clear()
        self._queue_samples.clear()
        self._diag_transcripts.clear()
        self._diag_base = {"vad_starts": self._vad_starts, "vad_ends": self._vad_ends,
                           "seg_accepted": self._seg_accepted, "seg_dropped": self._seg_dropped_short,
                           "recv": self._recv, "chunks": self._nchunks,
                           "drop_speaking": self._drop_speaking, "utterances": self.utterances}
        self._diag_started = time.time()

    def diag_window(self) -> dict:
        """Stats since the last diag_reset(): RMS distribution, thresholds/floor, VAD + segment counts, STT and
        queue-wait distributions, and the transcripts produced in this window."""
        b = self._diag_base
        enter_thr, exit_thr = self._thresholds()
        rms = list(self._rms_samples)
        return {
            "elapsed_s": round(time.time() - self._diag_started, 1) if self._diag_started else None,
            "rms": self._stats(rms),
            "noise_floor": round(self._floor, 1), "enter_thr": round(enter_thr, 1),
            "exit_thr": round(exit_thr, 1), "adaptive": self.adaptive,
            "vad_starts": self._vad_starts - b.get("vad_starts", 0),
            "vad_ends": self._vad_ends - b.get("vad_ends", 0),
            "seg_accepted": self._seg_accepted - b.get("seg_accepted", 0),
            "seg_dropped": self._seg_dropped_short - b.get("seg_dropped", 0),
            "recv": self._recv - b.get("recv", 0), "chunks": self._nchunks - b.get("chunks", 0),
            "drop_speaking": self._drop_speaking - b.get("drop_speaking", 0),
            "utterances": self.utterances - b.get("utterances", 0),
            "stt_ms": self._stats(list(self._stt_samples)),
            "queue_wait_ms": self._stats(list(self._queue_samples)),
            "transcripts": list(self._diag_transcripts),
        }

    def _thresholds(self) -> tuple[float, float]:
        """(enter, exit) RMS thresholds. Fixed mode: both equal `rms_threshold` (no hysteresis, old behavior).
        Adaptive mode: derived from the learned noise floor, with exit < enter for hysteresis."""
        if not self.adaptive:
            return float(self.rms_threshold), float(self.rms_threshold)
        enter = max(float(self.rms_min), min(float(self.rms_max), self._floor * self.enter_k))
        exit_t = max(float(self.rms_min) * 0.8, min(enter - 1.0, self._floor * self.exit_k))
        return enter, exit_t

    def _update_floor(self, rms: float) -> None:
        """Learn the background noise floor — ONLY during confident silence and never mid-utterance. (Our own
        TTS is already dropped by the echo gate before this runs, so the floor can't learn our voice.) Clamped
        so sustained noise can never raise the floor to speech level and deafen the VAD."""
        if not self.adaptive or self._in_speech:
            return
        _enter, exit_t = self._thresholds()
        if rms < exit_t:   # this chunk reads as silence -> fold it into the floor estimate
            a = self._floor_alpha
            self._floor = max(float(self.rms_min), min(float(self.rms_max),
                                                       (1.0 - a) * self._floor + a * float(rms)))

    def debug(self) -> dict:
        enter_thr, exit_thr = self._thresholds()
        return {"chunks": self._nchunks, "recv": self._recv, "max_rms": self._max_rms,
                "rms_threshold": self.rms_threshold, "in_speech": self._in_speech, "queued": len(self._jobs),
                # adaptive VAD state
                "adaptive": self.adaptive, "noise_floor": round(self._floor, 1),
                "enter_thr": round(enter_thr, 1), "exit_thr": round(exit_thr, 1),
                "utterances": self.utterances, "last_text": self._last_text, "last_error": self.last_error,
                "drop_empty": self._drop_empty, "drop_speaking": self._drop_speaking,
                "drop_rms": self._drop_rms, "whisper_loaded": self._whisper is not None,
                "stt_device": self._stt_device_loaded,
                # per-stage diagnostics
                "vad_starts": self._vad_starts, "vad_ends": self._vad_ends,
                "seg_accepted": self._seg_accepted, "seg_dropped_short": self._seg_dropped_short,
                "stt_runs": self._stt_runs, "stt_fail": self._stt_fail,
                "last_seg_dur": round(self._last_seg_dur, 2), "last_stt_ms": round(self._last_stt_ms, 1),
                "last_queue_wait_ms": round(self._last_queue_wait_ms, 1),
                "bargein": self._bargein, "bargein_fires": self.bargein_fires,
                "recent": list(self._events)}

    def attach(self, hub) -> None:
        self._running = True
        self._worker = threading.Thread(target=self._stt_loop, name="audio-stt", daemon=True)
        self._worker.start()
        if self.on_critical and self._bargein:
            self._bi_thread = threading.Thread(target=self._bargein_loop, name="audio-bargein", daemon=True)
            self._bi_thread.start()
        self._unsub_audio = hub.subscribe_audio(self._on_chunk)

    def stop(self) -> None:
        """Unsubscribe from the hub and tear the worker threads down cleanly (wake + join) so a server
        restart leaves no duplicate subscribers or leaked threads."""
        self._running = False
        if self._unsub_audio is not None:   # remove our hub subscriber FIRST so no new chunks arrive
            try:
                self._unsub_audio()
            except Exception:  # noqa: BLE001
                pass
            self._unsub_audio = None
        with self._cv:
            self._cv.notify_all()
        self._bi_event.set()   # wake the barge-in worker so it can exit
        for t in (self._worker, self._bi_thread):
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=2.0)
        self._worker = None
        self._bi_thread = None

    def _on_chunk(self, chunk) -> None:
        self._recv += 1
        pcm = chunk.pcm
        if not pcm:
            self._drop_empty += 1
            return
        # Echo gate: while the robot is speaking its own TTS, drop mic audio (and abandon any in-progress
        # utterance) so we don't transcribe ourselves. Saves CPU too — no STT on our own voice.
        from . import audio_state
        if audio_state.is_speaking():
            self._drop_speaking += 1
            with self._lock:
                self._in_speech = False
                self._buf = bytearray()
            # Barge-in: while WE talk, the normal VAD stays muted (don't transcribe ourselves), but we DO feed
            # the bounded barge-in window so a STOP/QUIET can interrupt us. Cheap: append + trim + signal only.
            if self.on_critical and self._bargein:
                self._bi_push(pcm)
            return
        elif self._bi_buf:
            # Not speaking anymore — drop any stale barge-in audio so detection only ever sees fresh input.
            with self._bi_lock:
                self._bi_buf = bytearray()
        try:
            rms = audioop.rms(pcm, 2)
        except Exception as e:  # noqa: BLE001
            self._drop_rms += 1
            self.last_error = f"rms: {type(e).__name__}: {e}"
            return
        now = time.monotonic()
        self._nchunks += 1
        if rms > self._max_rms:
            self._max_rms = rms
        self._rms_samples.append(rms)   # window-scoped RMS distribution (diag)
        # Adaptive floor learns from silence (no-op in fixed mode); enter/exit hysteresis decides "voiced".
        self._update_floor(rms)
        enter_thr, exit_thr = self._thresholds()
        voiced = rms >= (exit_thr if self._in_speech else enter_thr)

        with self._lock:
            if voiced:
                if not self._in_speech:
                    self._in_speech = True
                    self._speech_start = now
                    self._buf = bytearray()
                    self._vad_starts += 1
                    self._ev("vad_started", rms=rms)
                self._buf += pcm
                self._last_voice_ts = now
            elif self._in_speech:
                self._buf += pcm  # keep trailing audio so words aren't clipped
                # end of utterance? trailing silence past the hangover window
                if now - self._last_voice_ts >= self.hang_s:
                    self._finish(now)
            # safety: cap runaway utterances
            if self._in_speech and (now - self._speech_start) >= self.max_utt_s:
                self._finish(now)

    def _finish(self, now: float) -> None:
        dur = now - self._speech_start
        seg = bytes(self._buf)
        self._buf = bytearray()
        self._in_speech = False
        self._last_seg_dur = dur
        self._vad_ends += 1
        # Require a real chunk of speech (>= min_speech_s AND >= ~0.5s of samples) so we don't run STT on a
        # stray click/cough — those are where Whisper hallucinates phantom phrases.
        if dur >= self.min_speech_s and len(seg) >= int(self.sample_rate * 0.5) * 2:
            self._seg_accepted += 1
            self._ev("vad_ended", dur=round(dur, 2), bytes=len(seg), accepted=True)
            with self._cv:
                # Cap the backlog: CPU Whisper is slower than speech arrives, so drop the oldest rather than
                # lag minutes behind. Keep the most recent utterances (what the user just said). Each job
                # carries its enqueue time so the STT loop can report queue-wait latency.
                while len(self._jobs) >= self.queue_max:
                    self._jobs.popleft()
                self._jobs.append((seg, time.monotonic()))
                self._cv.notify()
        else:
            self._seg_dropped_short += 1
            self._ev("vad_ended", dur=round(dur, 2), bytes=len(seg), accepted=False)

    def _get_whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            # base.en is markedly more accurate on English than the multilingual base at the same speed.
            model = os.environ.get("AUTOBOT_STT_MODEL", "base.en")
            device = os.environ.get("AUTOBOT_STT_DEVICE", "cpu")        # "cuda" if you have spare VRAM
            compute = os.environ.get("AUTOBOT_STT_COMPUTE", "int8" if device == "cpu" else "float16")
            try:
                self._whisper = WhisperModel(model, device=device, compute_type=compute)
                self._stt_device_loaded = f"{device}:{compute}:{model}"
            except Exception as e:  # noqa: BLE001 - bad device/model -> fall back to safe CPU base.en
                self._stt_device_loaded = f"cpu:int8:base.en (FELL BACK from {device}: {type(e).__name__})"
                self._whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
        return self._whisper

    def _warm(self) -> None:
        """Pre-load + warm the Whisper model at startup so the FIRST real utterance (and the first STOP) is not
        a cold-start (cold GPU transcribe measured ~4.7s; warm is sub-second). Shared by normal + barge-in STT."""
        try:
            import numpy as np
            m = self._get_whisper()
            with self._whisper_lock:
                segs, _ = m.transcribe(np.zeros(8000, dtype="float32"), language="en", vad_filter=False)
                list(segs)
            self._ev("stt_warmed", device=self._stt_device_loaded)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"stt warm: {type(e).__name__}: {e}"
            self._ev("stt_warm_failed", error=self.last_error)

    def _stt_loop(self) -> None:
        self._warm()
        while self._running:
            with self._cv:
                while self._running and not self._jobs:
                    self._cv.wait(timeout=1.0)
                if not self._running:
                    break
                seg, enq_ts = self._jobs.popleft()
            self._last_queue_wait_ms = (time.monotonic() - enq_ts) * 1000.0
            self._queue_samples.append(self._last_queue_wait_ms)
            self._stt_runs += 1
            self._ev("stt_started", queue_wait_ms=round(self._last_queue_wait_ms, 1))
            t0 = time.monotonic()
            try:
                text = self._transcribe(seg)
            except Exception as e:  # noqa: BLE001
                self._stt_fail += 1
                self.last_error = f"{type(e).__name__}: {e}"
                self._ev("stt_failed", error=self.last_error)
                continue
            self._last_stt_ms = (time.monotonic() - t0) * 1000.0
            self._stt_samples.append(self._last_stt_ms)
            self._ev("transcript_produced", text=(text or "")[:60], ms=round(self._last_stt_ms, 1))
            self._last_text = text or self._last_text
            if text:
                self._diag_transcripts.append(text)
            if text and self.on_utterance:
                self.utterances += 1
                try:
                    self.on_utterance(text)
                except Exception:  # noqa: BLE001
                    pass

    def _transcribe(self, pcm: bytes) -> str:
        import numpy as np
        model = self._get_whisper()
        # The Air 2 mic is quiet — normalize each utterance to a healthy level so Whisper can read it.
        try:
            rms = audioop.rms(pcm, 2)
            if 0 < rms < 3000:
                pcm = audioop.mul(pcm, 2, min(12.0, 3000.0 / rms))   # saturating gain
        except Exception:  # noqa: BLE001
            pass
        audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        # vad_filter=False on purpose: we already segment with our own energy VAD above, and faster-whisper's
        # internal silero VAD pulls in onnxruntime, which crashes under numpy 2 in some envs.
        # Shared lock: normal STT and barge-in STT must never hit the one Whisper model concurrently.
        with self._whisper_lock:
            segments, _info = model.transcribe(audio, language="en", vad_filter=False,
                                               no_speech_threshold=0.6, log_prob_threshold=-1.0,
                                               condition_on_previous_text=False)
            segments = list(segments)   # force the generator to run inside the lock (lazy decode otherwise)
        # Drop low-confidence / non-speech segments (where Whisper invents phantom phrases on noise).
        kept = []
        for s in segments:
            txt = (s.text or "").strip()
            if not txt:
                continue
            if getattr(s, "no_speech_prob", 0.0) > 0.6:
                continue
            if getattr(s, "avg_logprob", 0.0) < -1.0:
                continue
            kept.append(txt)
        text = " ".join(kept).strip()
        return text if not _is_hallucination(text) else ""

    # --- barge-in (interrupt our own TTS with STOP/QUIET) ---
    def _bi_push(self, pcm: bytes) -> None:
        """Append the newest audio to the bounded single-window barge-in buffer and signal the worker. This is
        the only barge-in work done on the MediaHub callback thread — no STT, no blocking, no awaiting. The
        buffer holds at most one rolling window (oldest bytes dropped), so the worker always sees freshest."""
        with self._bi_lock:
            self._bi_buf += pcm
            maxb = int(self.sample_rate * self._bi_window_s) * 2
            if len(self._bi_buf) > maxb:
                del self._bi_buf[:len(self._bi_buf) - maxb]
        self._bi_event.set()

    def _bargein_classify(self, pcm: bytes) -> Optional[str]:
        """PCM window -> 'STOP'|'QUIET'|None. Energy-gated (above likely echo level), then STT, then the NARROW
        barge-in matcher, then self-echo rejection against the in-flight TTS text. Pure-ish (no side effects
        beyond a diagnostic event); used by the worker and unit-testable in isolation."""
        from . import audio_state, critical_words
        try:
            if audioop.rms(pcm, 2) < self._bi_min_rms:
                return None
        except Exception:  # noqa: BLE001
            return None
        try:
            text = self._transcribe(pcm)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"bargein stt: {type(e).__name__}: {e}"
            return None
        if not text:
            return None
        intent = critical_words.match_barge_in(text)
        if not intent:
            return None
        if critical_words.is_self_echo(text, audio_state.current_text()):
            self._ev("bargein_rejected_echo", text=text[:40])
            return None
        return intent

    def _bargein_loop(self) -> None:
        """Worker: when the robot is speaking, classify the freshest barge-in window (throttled) and fire
        on_critical the instant a STOP/QUIET lands. STT runs HERE, off the media thread."""
        from . import audio_state
        while self._running:
            if not self._bi_event.wait(timeout=0.5):
                continue
            self._bi_event.clear()
            if not self._running:
                break
            if not audio_state.is_speaking():
                continue
            now = time.monotonic()
            if now - self._bi_last_run < self._bi_min_interval:
                continue
            self._bi_last_run = now
            with self._bi_lock:
                snap = bytes(self._bi_buf)
            if len(snap) < int(self.sample_rate * 0.5) * 2:   # need ~0.5s to classify
                continue
            intent = self._bargein_classify(snap)
            if not intent:
                continue
            self.bargein_fires += 1
            self._ev("bargein_fired", intent=intent)
            with self._bi_lock:
                self._bi_buf = bytearray()
            try:
                self.on_critical(intent)   # callback does the thread-safe loop handoff (no async here)
            except Exception:  # noqa: BLE001
                pass
