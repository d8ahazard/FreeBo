"""agent_next_2 §5 — priority-first, exactly-once STOP.

The master STOP must dispatch the TRUE link E-STOP exactly once and not behind an ordinary transport stop, and
each STOP source (voice / barge-in / API / tool) must produce exactly one process transition + one link E-STOP.
"""
from __future__ import annotations

import asyncio

import pytest

from autobot.robot.mock_link import MockRobotLink


class _RecLink(MockRobotLink):
    """Records the ORDER of estop vs stop calls so we can assert the priority E-STOP isn't delayed."""
    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    async def estop(self, generation=None, epoch=None):
        self.calls.append("estop")
        return {"ok": True, "sent_to_agora": True, "local_latch_set": True,
                "initial_zero_sdk_send_succeeded": True, "latched": True,
                "generation": generation, "epoch": epoch}

    async def stop(self):
        self.calls.append("stop")
        return await super().stop()


def _brain(tmp_path, link):
    from autobot.brain.agent import AgentBrain
    from autobot.brain.identity import Identity
    from autobot.brain.memory import Memory
    from autobot.config import SETTINGS

    SETTINGS.update(setup_complete=True, autonomy="auto", allow_motion=True, allow_think=True, talk_enabled=False)

    async def emit(_ev):
        return None

    return AgentBrain(SETTINGS, emit, link, Memory(base_dir=str(tmp_path / "mem")),
                      Identity(emit=lambda _ev: None))


async def test_master_stop_issues_exactly_one_priority_estop(tmp_path):
    link = _RecLink()
    brain = _brain(tmp_path, link)
    res = await brain.emergency_stop("api STOP", cancel_tts=True, master=True)
    assert res["master"] is True and res["transport_dispatch_succeeded"] is True
    # exactly one link E-STOP; emergency_stop itself does NOT issue an ordinary stop ahead of it (no active action)
    assert link.calls.count("estop") == 1
    assert link.calls == ["estop"]                       # estop only; no preceding ordinary stop
    assert brain.safety.is_master_inhibited() and brain.safety.is_latched()


async def test_voice_stop_dispatches_master_stop_exactly_once(tmp_path, monkeypatch):
    link = _RecLink()
    brain = _brain(tmp_path, link)
    brain._loop = asyncio.get_running_loop()             # feed_speech hands off to this loop

    calls = {"estop": 0}
    posts: list = []

    async def fake_estop(reason, *, cancel_tts=False, behavior_stop=False, latch=False, master=False):
        calls["estop"] += 1
        return {"ok": True, "master": master}

    monkeypatch.setattr(brain, "emergency_stop", fake_estop)
    monkeypatch.setattr(brain, "_post", lambda kind, data=None: posts.append((kind, data)))

    brain.feed_speech("stop", speaker="owner", addressed=True)
    await asyncio.sleep(0.05)                             # let the scheduled coroutine run
    assert calls["estop"] == 1                            # exactly once
    assert not any(k == "command" for k, _ in posts)      # NOT also enqueued as a command (no double STOP)
