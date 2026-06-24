"""AudioSink — Phase 0.2 baseline: per-stage diagnostics + the existing energy-VAD segmentation behavior.

These pin CURRENT behavior before the Phase 0.4 adaptive-VAD change. They drive `_on_chunk` directly with a
fake clock (no MediaHub, no worker thread, no whisper), so they exercise segmentation/diagnostics only.
"""
from __future__ import annotations

import types

import pytest

import autobot.brain.audio_sink as asink
from autobot.brain.audio_sink import AudioSink, _is_hallucination
from autobot.robot.media_hub import MediaHub


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return self.t


def _chunk(amp: int, seconds: float, rate: int = 16000):
    n = int(rate * seconds)
    pcm = (int(amp).to_bytes(2, "little", signed=True)) * n
    return types.SimpleNamespace(pcm=pcm)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(asink, "time", c)
    # never gate on our own TTS in these tests unless asked
    monkeypatch.setattr("autobot.brain.audio_state.is_speaking", lambda: False)
    return c


def _sink():
    return AudioSink(rms_threshold=500, min_speech_s=0.4, hang_s=0.7, max_utt_s=12.0)


def test_speech_then_silence_produces_one_segment(clock):
    s = _sink()
    for _ in range(24):                 # ~1.2s of loud audio (> min_speech and > 0.5s of samples)
        s._on_chunk(_chunk(5000, 0.05))
        clock.t += 0.05
    clock.t += 0.8                       # trailing silence past the 0.7s hang
    s._on_chunk(_chunk(0, 0.05))         # endpoint fires
    d = s.debug()
    assert d["vad_starts"] == 1 and d["vad_ends"] == 1
    assert d["seg_accepted"] == 1 and d["seg_dropped_short"] == 0
    assert len(s._jobs) == 1
    assert any(e["stage"] == "vad_started" for e in d["recent"])
    assert any(e["stage"] == "vad_ended" and e["accepted"] for e in d["recent"])


def test_too_short_burst_is_dropped(clock):
    s = _sink()
    for _ in range(4):                   # ~0.2s — below min_speech_s
        s._on_chunk(_chunk(5000, 0.05))
        clock.t += 0.05
    clock.t += 0.8
    s._on_chunk(_chunk(0, 0.05))
    d = s.debug()
    assert d["vad_ends"] == 1 and d["seg_dropped_short"] == 1 and d["seg_accepted"] == 0
    assert len(s._jobs) == 0


def test_echo_gate_drops_audio_while_speaking(monkeypatch, clock):
    monkeypatch.setattr("autobot.brain.audio_state.is_speaking", lambda: True)
    s = _sink()
    s._on_chunk(_chunk(5000, 0.05))
    d = s.debug()
    assert d["drop_speaking"] == 1 and d["in_speech"] is False and d["vad_starts"] == 0


def test_quiet_audio_never_enters_speech(clock):
    s = _sink()
    for _ in range(24):
        s._on_chunk(_chunk(100, 0.05))   # below the 500 RMS threshold
        clock.t += 0.05
    d = s.debug()
    assert d["vad_starts"] == 0 and len(s._jobs) == 0


def test_audio_sink_start_stop_restart_no_leaks():
    hub = MediaHub()
    base = hub.stats()["audio_subs"]
    # with a barge-in worker too, both threads must be joined on stop
    s = AudioSink(on_critical=lambda i: None)
    s.attach(hub)
    assert hub.stats()["audio_subs"] == base + 1
    assert s._worker is not None and s._worker.is_alive()
    assert s._bi_thread is not None and s._bi_thread.is_alive()
    s.stop()
    assert hub.stats()["audio_subs"] == base
    assert s._worker is None and s._bi_thread is None
    # restart -> exactly one subscriber again, no leaked threads
    s2 = AudioSink(on_critical=lambda i: None)
    s2.attach(hub)
    assert hub.stats()["audio_subs"] == base + 1
    s2.stop()
    assert hub.stats()["audio_subs"] == base


def test_hallucination_filter():
    assert _is_hallucination("you") is True
    assert _is_hallucination("thank you.") is True
    assert _is_hallucination("...") is True
    assert _is_hallucination("turn left") is False


# --- Phase 0.4: adaptive noise floor ---

def _adaptive(monkeypatch, **kw):
    for v in ("AUTOBOT_STT_RMS", "AUTOBOT_STT_RMS_MIN", "AUTOBOT_STT_RMS_MAX",
              "AUTOBOT_STT_ENTER_K", "AUTOBOT_STT_EXIT_K", "AUTOBOT_STT_FLOOR_ALPHA"):
        monkeypatch.delenv(v, raising=False)
    return AudioSink(min_speech_s=0.4, hang_s=0.7, **kw)


def test_adaptive_enabled_by_default(monkeypatch):
    s = _adaptive(monkeypatch)
    assert s.adaptive is True
    enter, exit_t = s._thresholds()
    assert enter > exit_t


def test_fixed_when_threshold_arg_passed(monkeypatch):
    monkeypatch.delenv("AUTOBOT_STT_RMS", raising=False)
    s = AudioSink(rms_threshold=500)
    assert s.adaptive is False
    assert s._thresholds() == (500.0, 500.0)


def test_fixed_when_env_set(monkeypatch):
    monkeypatch.setenv("AUTOBOT_STT_RMS", "800")
    s = AudioSink()
    assert s.adaptive is False and s.rms_threshold == 800


def test_floor_learns_quiet_noise_and_stays_clamped(monkeypatch, clock):
    s = _adaptive(monkeypatch)
    start = s._floor
    for _ in range(120):                 # quiet hiss below the exit threshold
        s._on_chunk(_chunk(300, 0.05))
        clock.t += 0.05
    assert s._floor > start              # learned the room hiss
    assert s._floor <= s.rms_max         # but never to speech level
    assert s.debug()["vad_starts"] == 0  # quiet hiss never reads as speech


def test_floor_never_learns_speech_level(monkeypatch, clock):
    s = _adaptive(monkeypatch)
    floor0 = s._floor
    for _ in range(50):                  # sustained speech-level energy
        s._on_chunk(_chunk(5000, 0.05))
        clock.t += 0.05
    assert s._in_speech is True
    assert s._floor == floor0            # never folded speech into the noise floor


def test_hysteresis_keeps_speech_through_a_dip(monkeypatch, clock):
    s = _adaptive(monkeypatch)
    s._floor = 200.0                     # enter=500, exit=300
    s._on_chunk(_chunk(600, 0.05)); clock.t += 0.05      # cross enter -> speech
    for _ in range(5):                   # dip between exit and enter — must stay in speech
        s._on_chunk(_chunk(350, 0.05)); clock.t += 0.05
    assert s._in_speech is True and s.debug()["vad_ends"] == 0
    clock.t += 0.8                       # real silence past the hang
    s._on_chunk(_chunk(50, 0.05))
    assert s.debug()["vad_ends"] == 1
