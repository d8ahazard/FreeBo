"""P0 §4 — real reasoning cancellation + faculty inhibition.

A master STOP must (a) invalidate the in-flight reason cycle's generation, (b) cancel its task so a cycle
blocked in a provider await is torn down, and (c) prevent any stale result from producing side effects or
resurfacing post-RESUME. /api/tick + /api/chat (both -> brain.tick -> _reason) are gated by check_think.
"""
from __future__ import annotations

import asyncio

import pytest

from autobot.brain.agent import ReasonCancelled
from autobot.robot.mock_link import MockRobotLink


def _brain(tmp_path):
    from autobot.brain.agent import AgentBrain
    from autobot.brain.identity import Identity
    from autobot.brain.memory import Memory
    from autobot.config import SETTINGS

    # allow_video=False keeps perception text-only (no ffmpeg snapshot) so the cortex round is reached fast.
    SETTINGS.update(setup_complete=True, autonomy="auto", allow_motion=False, allow_think=True,
                    allow_video=False, talk_enabled=False, ai_provider="openai",
                    ai_base_url="http://localhost:9", ai_api_key="x", ai_model="m")

    async def emit(_ev):
        return None

    brain = AgentBrain(SETTINGS, emit, MockRobotLink(),
                       Memory(base_dir=str(tmp_path / "mem")), Identity(emit=lambda _ev: None))
    return brain, SETTINGS


def test_reason_guard_raises_after_generation_bump(tmp_path):
    brain, s = _brain(tmp_path)
    snap = s.snapshot()
    token = brain._reason_gen
    brain._reason_guard(token, snap)              # alive: no exception
    brain._reason_gen += 1                          # a master STOP bumped the generation
    with pytest.raises(ReasonCancelled):
        brain._reason_guard(token, snap)


def test_reason_guard_raises_when_master_inhibited(tmp_path):
    brain, s = _brain(tmp_path)
    snap = s.snapshot()
    token = brain._reason_gen
    brain.safety.begin_master_stop()                # the only way to assert the master inhibit (tokenized)
    with pytest.raises(ReasonCancelled):
        brain._reason_guard(token, snap)


async def test_reason_returns_cancelled_when_think_inhibited(tmp_path):
    brain, s = _brain(tmp_path)
    brain.safety.begin_master_stop()                # Think no longer permitted
    res = await brain._reason("manual", s.snapshot())
    assert res.get("cancelled") is True and res.get("ok") is False


async def test_master_stop_cancels_the_inflight_reason_task(tmp_path):
    brain, _s = _brain(tmp_path)

    async def _long():
        await asyncio.sleep(30)

    task = asyncio.create_task(_long())
    brain._reason_tasks.add(task)                    # registered like a live reason invocation
    await asyncio.sleep(0)                           # let it start
    g0 = brain._reason_gen
    await brain.emergency_stop("test", master=True)
    assert brain._reason_gen == g0 + 1              # generation invalidated
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_stop_while_provider_blocked_yields_cancelled_no_drive(tmp_path, monkeypatch):
    """The strongest case: a reason cycle blocked inside the provider await is cancelled by a master STOP and
    produces a cancelled result — never a tool call / drive."""
    brain, s = _brain(tmp_path)
    import autobot.brain.agent as agent_mod
    from autobot.brain.perception import Observation

    entered = asyncio.Event()
    release = asyncio.Event()

    async def ready_perceive(link, want_image=True):
        return Observation(telemetry={"ok": True, "connected": True, "awake": True})

    async def blocking_chat(self, messages, tools=None):
        entered.set()
        await release.wait()                        # block until the test releases (it won't, before STOP)
        raise AssertionError("provider should have been cancelled before returning")

    monkeypatch.setattr(agent_mod, "perceive", ready_perceive, raising=False)
    # agent_next_2 §6.7: force the tool-calling cortex path deterministically (no VLM/omni/hybrid branch) so this
    # test ALWAYS reaches the mocked provider — it is mandatory, never skipped.
    monkeypatch.setattr(agent_mod, "vlm_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod, "omni_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod, "hybrid_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod.OpenAICompatibleClient, "chat", blocking_chat, raising=False)

    tick = asyncio.create_task(brain.tick(force=True))
    await asyncio.wait_for(entered.wait(), timeout=10.0)        # cycle is now blocked in the provider (mandatory)
    await brain.emergency_stop("barge-in", master=True)        # cancels the blocked task
    res = await asyncio.wait_for(tick, timeout=5.0)
    assert res.get("cancelled") is True
    # the robot never received a drive from the cancelled cycle
    assert brain.link.state.get("last_drive") in (None, (0.0, 0.0))


class _ToolResult:
    def __init__(self):
        self.tool_calls = [{"id": "c1", "name": "drive", "arguments": {"direction": "forward", "duration": 0.3}}]
        self.content = ""


async def test_think_off_during_provider_await_cancels_before_tool(tmp_path, monkeypatch):
    # agent_next_2 §6.2: a live Think-off mid-cycle (during the provider await) must cancel at the next boundary
    # BEFORE the returned drive tool call executes.
    brain, s = _brain(tmp_path)
    import autobot.brain.agent as agent_mod
    from autobot.brain.perception import Observation

    s.update(allow_motion=True)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def ready_perceive(link, want_image=True):
        return Observation(telemetry={"ok": True, "connected": True, "awake": True})

    async def chat(self, messages, tools=None):
        entered.set()
        await release.wait()
        return _ToolResult()

    monkeypatch.setattr(agent_mod, "perceive", ready_perceive, raising=False)
    monkeypatch.setattr(agent_mod, "vlm_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod, "omni_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod, "hybrid_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod.OpenAICompatibleClient, "chat", chat, raising=False)

    tick = asyncio.create_task(brain.tick(force=True))
    await asyncio.wait_for(entered.wait(), timeout=10.0)
    s.update(allow_think=False)          # flip Think off mid-await (no master STOP)
    release.set()
    res = await asyncio.wait_for(tick, timeout=5.0)
    assert res.get("cancelled") is True
    assert brain.link.state.get("last_drive") in (None, (0.0, 0.0))   # the drive tool never ran


async def test_concurrent_running_and_waiting_reason_both_cancelled(tmp_path, monkeypatch):
    # agent_next_2 §6.1: one running (provider-blocked) reason + one waiting on the lock; a master STOP cancels
    # BOTH (a waiter must not hide the blocked owner).
    brain, s = _brain(tmp_path)
    import autobot.brain.agent as agent_mod
    from autobot.brain.perception import Observation

    entered = asyncio.Event()
    release = asyncio.Event()

    async def ready_perceive(link, want_image=True):
        return Observation(telemetry={"ok": True, "connected": True, "awake": True})

    async def blocking_chat(self, messages, tools=None):
        entered.set()
        await release.wait()
        raise AssertionError("should be cancelled")

    monkeypatch.setattr(agent_mod, "perceive", ready_perceive, raising=False)
    monkeypatch.setattr(agent_mod, "vlm_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod, "omni_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod, "hybrid_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod.OpenAICompatibleClient, "chat", blocking_chat, raising=False)

    t1 = asyncio.create_task(brain.tick(force=True))     # becomes the lock owner, blocks in provider
    await asyncio.wait_for(entered.wait(), timeout=10.0)
    t2 = asyncio.create_task(brain.tick(force=True))     # waits on the reason lock
    await asyncio.sleep(0.05)
    assert len(brain._reason_tasks) >= 2                  # both tracked
    await brain.emergency_stop("stop", master=True)
    r1 = await asyncio.wait_for(t1, timeout=5.0)
    r2 = await asyncio.wait_for(t2, timeout=5.0)
    assert r1.get("cancelled") is True and r2.get("cancelled") is True
