"""Core body-control skill: drive/say/eyes go through the safety floor + the link."""
from __future__ import annotations

from conftest import settings

from autobot.brain.safety import SafetyFloor
from autobot.brain.skills.base import SkillContext
from autobot.brain.skills.core import CoreSkill
from autobot.robot.mock_link import MockRobotLink


def _ctx(s):
    sf = SafetyFloor()
    sf.begin_tick()

    async def emit(_ev):
        return None

    return SkillContext(link=MockRobotLink(), settings=s, safety=sf, memory=None, identity=None, emit=emit)


def _skill(ctx):
    sk = CoreSkill()
    sk.available(ctx)
    return sk


async def test_drive_blocked_in_manual():
    sk = _skill(_ctx(settings(autonomy="manual")))
    r = await sk._drive({"direction": "forward"})
    assert not r["ok"] and "blocked" in r


async def test_drive_ok_and_clamped_in_auto():
    sk = _skill(_ctx(settings(autonomy="auto", allow_motion=True, max_speed=0.6)))
    r = await sk._drive({"direction": "forward", "duration": 1.0, "speed": 1.0})
    assert r["ok"]
    assert abs(r["drove"]["ly"]) <= 0.6 + 1e-6


async def test_drive_needs_direction_or_vector():
    sk = _skill(_ctx(settings(autonomy="auto", allow_motion=True)))
    r = await sk._drive({"direction": "nosuchway"})
    assert not r["ok"]


async def test_say_blocked_when_talk_off():
    sk = _skill(_ctx(settings(talk_enabled=False)))
    r = await sk._say({"text": "hi"})
    assert not r["ok"]


async def test_say_ok_when_talk_on():
    sk = _skill(_ctx(settings(talk_enabled=True)))
    r = await sk._say({"text": "hello there"})
    assert r["ok"]


async def test_set_eyes():
    sk = _skill(_ctx(settings()))
    r = await sk._set_eyes({"animation": "happy"})
    assert r["ok"] and r["eyes"] == "happy"


async def test_stop():
    sk = _skill(_ctx(settings()))
    r = await sk._stop({})
    assert r["ok"] and r["stopped"]
