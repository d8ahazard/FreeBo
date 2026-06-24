"""Behavior controller — P0-R3.3: roaming is decided PURELY by the user-visible mode (no hidden env switch).

  observe -> ADJUST/observe (never roams)   explore -> ROAM (greet/patrol/idle-roam)
"""
from __future__ import annotations

from conftest import settings

from autobot.brain.behavior import ADJUST, HOLD, ROAM, BehaviorController


def test_observe_mode_stays_put():
    bc = BehaviorController()
    assert bc.current.scope == ADJUST   # starts calm
    b = bc.decide(settings(mode="observe"), resting=False, present_people=[])
    assert b.scope == ADJUST and b.intent == "observe"


def test_observe_does_not_roam_even_with_people():
    bc = BehaviorController()
    b = bc.decide(settings(mode="observe"), resting=False, present_people=["Ben"])
    assert b.scope == ADJUST and b.intent == "observe"


def test_explore_mode_roams_by_default():
    bc = BehaviorController()
    b = bc.decide(settings(mode="explore"), resting=False, present_people=[])
    assert b.scope == ROAM and b.intent == "explore_active"


def test_greet_overrides_explore():
    bc = BehaviorController()
    b = bc.decide(settings(mode="explore"), resting=False, present_people=["Ben"])
    assert b.scope == ROAM and b.intent == "greet"


def test_voice_stop_holds():
    bc = BehaviorController()
    bc.set_voice_intent("stopped", seconds=60.0)
    b = bc.decide(settings(mode="explore"), resting=False, present_people=[])
    assert b.scope == HOLD


def test_resting_holds():
    bc = BehaviorController()
    b = bc.decide(settings(mode="explore"), resting=True, present_people=[])
    assert b.scope == HOLD
