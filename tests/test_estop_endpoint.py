"""P0-R4 item 1 — prove the real E-STOP routing.

The historical bug: `OverseerGate` inherited the base `RobotLink.estop()` (which degrades to `stop()`), so a
master STOP never reached the link's true latched E-STOP. These tests prove (a) the gate explicitly delegates
estop/estop_reset to the inner link even while overseer mode is ON (safety ops are never intercepted as
proposals), and (b) `POST /api/estop` reaches the inner link's `estop()` and never falls back to plain stop.
"""
from __future__ import annotations

import os

import pytest

from autobot.config import Settings
from autobot.robot.overseer_gate import OverseerGate, ProposalStore


class _RecLink:
    """Minimal fake inner link recording estop/stop/estop_reset (not a full RobotLink — gate only calls these)."""
    def __init__(self):
        self.estop_calls: list = []
        self.estop_reset_calls: list = []
        self.stop_calls = 0

    async def estop(self, generation=None):
        self.estop_calls.append(generation)
        return {"ok": True, "initial_zero_sdk_send_succeeded": True, "latched": True, "generation": generation}

    async def estop_reset(self, generation=None):
        self.estop_reset_calls.append(generation)
        return {"ok": True, "latched": False, "generation": generation}

    async def stop(self):
        self.stop_calls += 1
        return {"ok": True}


def _gate(overseer: bool):
    s = Settings()
    s.update(overseer=overseer)
    inner = _RecLink()
    return OverseerGate(inner, s, ProposalStore()), inner


async def test_gate_estop_delegates_even_when_overseer_on():
    gate, inner = _gate(overseer=True)
    r = await gate.estop(generation=7)
    assert inner.estop_calls == [7]          # reached the real link
    assert inner.stop_calls == 0             # did NOT degrade to plain stop()
    assert r["latched"] is True


async def test_gate_estop_reset_delegates():
    gate, inner = _gate(overseer=True)
    await gate.estop_reset(generation=7)
    assert inner.estop_reset_calls == [7]


@pytest.mark.asyncio
async def test_api_estop_reaches_inner_estop_through_gate(monkeypatch):
    os.environ["AUTOBOT_ROBOT_LINK"] = "mock"
    from autobot.web import server

    inner = server.brain.link._inner  # the real link wrapped by the brain's OverseerGate
    calls = {"estop": [], "stop": 0}

    async def fake_estop(generation=None):
        calls["estop"].append(generation)
        return {"ok": True, "initial_zero_sdk_send_succeeded": True, "latched": True, "generation": generation}

    async def fake_stop():
        calls["stop"] += 1
        return {"ok": True}

    monkeypatch.setattr(inner, "estop", fake_estop, raising=False)
    monkeypatch.setattr(inner, "stop", fake_stop, raising=False)

    resp = await server.api_estop()
    # The core fix: estop() is reached through the gate (the old bug degraded to stop() and never called
    # estop at all). The executor's preempt may legitimately issue its own bounded stop() — that's fine; what
    # must NOT happen is the estop PATH degrading to stop instead of the real latched estop.
    assert calls["estop"], "POST /api/estop must reach the inner link's estop() through OverseerGate"
    assert calls["estop"][0] is not None, "estop must carry the authoritative generation"
    assert server.brain.safety.is_master_inhibited() is True
    assert server.brain.safety.is_latched() is True
    # honest response shape (amendment A): independent local vs transport facts
    body = resp.body.decode() if hasattr(resp, "body") else ""
    assert "local_inhibit_asserted" in body and "transport_dispatch_succeeded" in body
