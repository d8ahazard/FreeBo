"""Tiny shared audio state — the echo gate.

When the robot is speaking its own TTS through its speaker, its microphone hears it and STT transcribes it,
so the robot "talks to itself" / reacts to its own voice. There's no hardware echo cancellation on this path,
so we gate STT: the speech path marks a "speaking until" window (clip duration + a short tail) and the STT
path (AudioSink + the agent's heard-speech intake) ignores audio during it. Process-wide, thread-safe.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

_lock = threading.Lock()
_generation = 0                          # monotonic playback id; bumps each new clip (stale-completion guard)
_speaking_until = 0.0
_current_text = ""                       # the in-flight TTS text (for barge-in self-echo rejection)
_canceller: Optional[Callable[[], None]] = None  # cancels the in-flight clip (set by the speak path)
# Extra window after the clip's audio ends, to cover speaker/room tail + mic buffering before we re-listen.
TAIL_S = 0.6


# Hard cap on a single gate window. A bad/garbled TTS clip duration (or a miscomputed WAV rate) must never be
# able to wedge the echo gate open for minutes and deafen the robot. No real TTS clip is longer than this.
MAX_GATE_S = 20.0


def begin_playback(text: str, seconds: float, canceller: Optional[Callable[[], None]] = None) -> int:
    """Start a NEW playback generation (replacing any prior): open the echo gate for ~`seconds` (+tail), record
    the spoken `text` (barge-in self-echo rejection), and store the `canceller`. Returns the generation id so
    the caller can later clear/cancel ONLY its own clip (a stale completion from an older clip is a no-op)."""
    global _generation, _speaking_until, _current_text, _canceller
    seconds = min(max(0.0, seconds), MAX_GATE_S)
    with _lock:
        _generation += 1
        gen = _generation
        _speaking_until = time.time() + seconds + TAIL_S
        _current_text = text or ""
        _canceller = canceller
    return gen


def set_canceller(generation: int, canceller: Optional[Callable[[], None]]) -> None:
    """Attach/replace the canceller for `generation` IF it is still the active clip (else no-op)."""
    global _canceller
    with _lock:
        if generation == _generation:
            _canceller = canceller


def current_generation() -> int:
    with _lock:
        return _generation


def mark_speaking(seconds: float, text: str = "", canceller: Optional[Callable[[], None]] = None) -> int:
    """Back-compat shim (tests / legacy callers): start a fresh playback generation. Returns the generation."""
    return begin_playback(text, seconds, canceller)


def is_speaking() -> bool:
    with _lock:
        return time.time() < _speaking_until


def current_text() -> str:
    """The in-flight TTS text (or '' once cleared) — for barge-in self-echo rejection."""
    with _lock:
        return _current_text


def clear(generation: Optional[int] = None) -> None:
    """Mark playback COMPLETE — reset the echo gate. If `generation` is given and is NOT the active clip, this
    is a no-op (a stale completion from an older clip must never clear a newer clip's gate)."""
    global _speaking_until, _current_text, _canceller
    with _lock:
        if generation is not None and generation != _generation:
            return
        _speaking_until = 0.0
        _current_text = ""
        _canceller = None


def cancel(generation: Optional[int] = None) -> None:
    """Barge-in / preempt: stop the in-flight TTS immediately. Resets the echo gate (so STT re-listens) and
    invokes the registered canceller (link playback flush). If `generation` is given and is NOT the active
    clip, this is a no-op (a stale cancel can't kill a newer clip). Idempotent — canceller fires at most once."""
    global _speaking_until, _current_text, _canceller
    with _lock:
        if generation is not None and generation != _generation:
            return
        cb = _canceller
        _canceller = None
        _speaking_until = 0.0
        _current_text = ""
    if cb is not None:
        try:
            cb()
        except Exception:  # noqa: BLE001 - a bad canceller must never wedge the gate
            pass


def wav_duration_s(wav_bytes: bytes | None) -> float:
    """Best-effort duration of a PCM WAV (16-bit). Falls back to 0 on anything unexpected."""
    if not wav_bytes:
        return 0.0
    try:
        import io
        import wave
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate() or 16000
            return frames / float(rate)
    except Exception:  # noqa: BLE001
        return 0.0
