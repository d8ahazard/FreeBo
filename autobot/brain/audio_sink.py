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
                 sample_rate: int = 16000, rms_threshold: int = 0,
                 min_speech_s: float = 0.0, hang_s: float = 0.0, max_utt_s: float = 0.0) -> None:
        # The Air 2 mic comes in quiet over G.711 (speech ~400-700 RMS), so the VAD threshold is low and
        # env-tunable; each utterance is gain-normalized before Whisper (below). All VAD timings are env
        # tunable so you can tighten/loosen segmentation without code changes.
        rms_threshold = rms_threshold or int(os.environ.get("AUTOBOT_STT_RMS", "900"))
        self.on_utterance = on_utterance
        self.sample_rate = sample_rate
        self.rms_threshold = rms_threshold
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
        self._whisper = None
        self.utterances = 0
        self.last_error: Optional[str] = None
        self._nchunks = 0
        self._max_rms = 0
        self._last_text = ""

    def debug(self) -> dict:
        return {"chunks": self._nchunks, "max_rms": self._max_rms, "rms_threshold": self.rms_threshold,
                "in_speech": self._in_speech, "queued": len(self._jobs), "utterances": self.utterances,
                "last_text": self._last_text, "last_error": self.last_error,
                "whisper_loaded": self._whisper is not None}

    def attach(self, hub) -> None:
        self._running = True
        self._worker = threading.Thread(target=self._stt_loop, name="audio-stt", daemon=True)
        self._worker.start()
        hub.subscribe_audio(self._on_chunk)

    def stop(self) -> None:
        self._running = False
        with self._cv:
            self._cv.notify_all()

    def _on_chunk(self, chunk) -> None:
        pcm = chunk.pcm
        if not pcm:
            return
        # Echo gate: while the robot is speaking its own TTS, drop mic audio (and abandon any in-progress
        # utterance) so we don't transcribe ourselves. Saves CPU too — no STT on our own voice.
        from . import audio_state
        if audio_state.is_speaking():
            with self._lock:
                self._in_speech = False
                self._buf = bytearray()
            return
        try:
            rms = audioop.rms(pcm, 2)
        except Exception:  # noqa: BLE001
            return
        now = time.monotonic()
        self._nchunks += 1
        if rms > self._max_rms:
            self._max_rms = rms
        voiced = rms >= self.rms_threshold

        with self._lock:
            if voiced:
                if not self._in_speech:
                    self._in_speech = True
                    self._speech_start = now
                    self._buf = bytearray()
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
        # Require a real chunk of speech (>= min_speech_s AND >= ~0.5s of samples) so we don't run STT on a
        # stray click/cough — those are where Whisper hallucinates phantom phrases.
        if dur >= self.min_speech_s and len(seg) >= int(self.sample_rate * 0.5) * 2:
            with self._cv:
                # Cap the backlog: CPU Whisper is slower than speech arrives, so drop the oldest rather than
                # lag minutes behind. Keep the most recent utterances (what the user just said).
                while len(self._jobs) >= self.queue_max:
                    self._jobs.popleft()
                self._jobs.append(seg)
                self._cv.notify()

    def _get_whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            # base.en is markedly more accurate on English than the multilingual base at the same speed.
            model = os.environ.get("AUTOBOT_STT_MODEL", "base.en")
            device = os.environ.get("AUTOBOT_STT_DEVICE", "cpu")        # "cuda" if you have spare VRAM
            compute = os.environ.get("AUTOBOT_STT_COMPUTE", "int8" if device == "cpu" else "float16")
            try:
                self._whisper = WhisperModel(model, device=device, compute_type=compute)
            except Exception:  # noqa: BLE001 - bad device/model -> fall back to safe CPU base.en
                self._whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
        return self._whisper

    def _stt_loop(self) -> None:
        while self._running:
            with self._cv:
                while self._running and not self._jobs:
                    self._cv.wait(timeout=1.0)
                if not self._running:
                    break
                seg = self._jobs.popleft()
            try:
                text = self._transcribe(seg)
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
                continue
            self._last_text = text or self._last_text
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
        segments, _info = model.transcribe(audio, language="en", vad_filter=False,
                                           no_speech_threshold=0.6, log_prob_threshold=-1.0,
                                           condition_on_previous_text=False)
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
