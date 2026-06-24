"""Phase 0.5 — cancellable TTS + critical-command barge-in (STOP/QUIET while the robot is talking)."""
from __future__ import annotations

import autobot.brain.audio_state as ast
from autobot.brain import critical_words as cw
from autobot.brain.audio_sink import AudioSink


def _pcm(amp: int, seconds: float, rate: int = 16000) -> bytes:
    return int(amp).to_bytes(2, "little", signed=True) * int(rate * seconds)


# --- critical_words ---

def test_barge_in_match_is_narrow():
    assert cw.match_barge_in("stop") == "STOP"
    assert cw.match_barge_in("be quiet") == "QUIET"
    assert cw.match_barge_in("shut up") == "QUIET"
    # broad/conversational phrases that commands.py would catch must NOT trip barge-in
    assert cw.match_barge_in("that's enough for now") is None
    assert cw.match_barge_in("what do you see over there") is None


def test_strip_reserved_prevents_self_trigger():
    out = cw.strip_reserved("Okay, I'll stop and be quiet now.")
    assert cw.match_barge_in(out) is None
    assert "stop" not in out.lower() and "quiet" not in out.lower()


def test_is_self_echo():
    assert cw.is_self_echo("stop", "please stop now") is True       # our own words
    assert cw.is_self_echo("stop", "hello there friend") is False   # external word


# --- audio_state cancellation ---

def test_audio_state_cancel_resets_gate_and_fires_canceller():
    calls = []
    ast.mark_speaking(5.0, text="hello world", canceller=lambda: calls.append(1))
    assert ast.is_speaking() and ast.current_text() == "hello world"
    ast.cancel()
    assert not ast.is_speaking() and ast.current_text() == ""
    assert calls == [1]
    ast.cancel()                 # idempotent — canceller does not fire again
    assert calls == [1]


# --- AudioSink barge-in classifier ---

def _sink(monkeypatch, transcript: str):
    s = AudioSink(on_critical=lambda i: None, rms_threshold=500)
    s._bi_min_rms = 100
    monkeypatch.setattr(s, "_transcribe", lambda pcm: transcript)
    return s


def test_bargein_classify_detects_stop(monkeypatch):
    ast.cancel()                 # clear any in-flight TTS text from other tests
    s = _sink(monkeypatch, "stop")
    assert s._bargein_classify(_pcm(5000, 0.6)) == "STOP"


def test_bargein_ignores_quiet_audio(monkeypatch):
    ast.cancel()
    s = _sink(monkeypatch, "stop")
    assert s._bargein_classify(_pcm(20, 0.6)) is None   # below the energy gate -> likely echo


def test_bargein_ignores_non_command(monkeypatch):
    ast.cancel()
    s = _sink(monkeypatch, "the cat is on the table")
    assert s._bargein_classify(_pcm(5000, 0.6)) is None


def test_bargein_rejects_self_echo(monkeypatch):
    ast.mark_speaking(5.0, text="okay I will stop now")   # our own TTS says a (would-be) trigger
    s = _sink(monkeypatch, "stop now")
    assert s._bargein_classify(_pcm(5000, 0.6)) is None
    ast.cancel()
