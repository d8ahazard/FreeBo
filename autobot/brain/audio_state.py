"""Tiny shared audio state — the echo gate.

When the robot is speaking its own TTS through its speaker, its microphone hears it and STT transcribes it,
so the robot "talks to itself" / reacts to its own voice. There's no hardware echo cancellation on this path,
so we gate STT: the speech path marks a "speaking until" window (clip duration + a short tail) and the STT
path (AudioSink + the agent's heard-speech intake) ignores audio during it. Process-wide, thread-safe.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_speaking_until = 0.0
# Extra window after the clip's audio ends, to cover speaker/room tail + mic buffering before we re-listen.
TAIL_S = 0.6


def mark_speaking(seconds: float) -> None:
    """Mark that the robot is speaking for ~`seconds` (clip duration); STT is muted until then + a tail."""
    global _speaking_until
    with _lock:
        _speaking_until = max(_speaking_until, time.time() + max(0.0, seconds) + TAIL_S)


def is_speaking() -> bool:
    with _lock:
        return time.time() < _speaking_until


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
