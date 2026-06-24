"""Behavior controller — Phase 0: roaming is OFF by default (calm companion baseline)."""
from __future__ import annotations

from conftest import settings

from autobot.brain.behavior import ADJUST, HOLD, ROAM, BehaviorController


def _bc(monkeypatch, active_explore: str | None):
    if active_explore is None:
        monkeypatch.delenv("AUTOBOT_ACTIVE_EXPLORE", raising=False)
    else:
        monkeypatch.setenv("AUTOBOT_ACTIVE_EXPLORE", active_explore)
    return BehaviorController()


def test_default_is_calm_observe(monkeypatch):
    bc = _bc(monkeypatch, None)
    assert bc.active_explore is False
    assert bc.current.scope == ADJUST
    b = bc.decide(settings(mode="explore"), resting=False, present_people=[])
    assert b.scope == ADJUST and b.intent == "observe"


def test_explicit_opt_in_roams(monkeypatch):
    bc = _bc(monkeypatch, "1")
    assert bc.active_explore is True
    b = bc.decide(settings(mode="explore"), resting=False, present_people=[])
    assert b.scope == ROAM and b.intent == "explore_active"


def test_greet_overrides_calm_default(monkeypatch):
    bc = _bc(monkeypatch, None)
    b = bc.decide(settings(mode="explore"), resting=False, present_people=["Ben"])
    assert b.scope == ROAM and b.intent == "greet"


def test_voice_stop_holds(monkeypatch):
    bc = _bc(monkeypatch, "1")
    bc.set_voice_intent("stopped", seconds=60.0)
    b = bc.decide(settings(mode="explore"), resting=False, present_people=[])
    assert b.scope == HOLD


def test_resting_holds_even_when_opted_in(monkeypatch):
    bc = _bc(monkeypatch, "1")
    b = bc.decide(settings(mode="explore"), resting=True, present_people=[])
    assert b.scope == HOLD
