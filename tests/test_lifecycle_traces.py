"""agent_next_4 §9 — focused lifecycle ORDERING traces (fast; no server/model import):
reason.lifecycle ordering + no stale completion after cancellation, speech.lifecycle ordering, and
control.transport ordering through a real RtmNode (faked _send)."""
from __future__ import annotations

import asyncio

import pytest

from autobot import observability as obs
from autobot.robot.mock_link import MockRobotLink


@pytest.fixture
def journal(tmp_path):
    j = obs.configure(str(tmp_path / "events.jsonl"), flush_interval=0.05)
    yield j
    obs.configure(None)


def _brain(tmp_path):
    from autobot.brain.agent import AgentBrain
    from autobot.brain.identity import Identity
    from autobot.brain.memory import Memory
    from autobot.config import SETTINGS
    SETTINGS.update(setup_complete=True, autonomy="auto", allow_motion=False, allow_think=True,
                    allow_video=False, talk_enabled=False, ai_provider="openai",
                    ai_base_url="http://localhost:9", ai_api_key="x", ai_model="m")

    async def emit(_ev):
        return None
    brain = AgentBrain(SETTINGS, emit, MockRobotLink(),
                       Memory(base_dir=str(tmp_path / "mem")), Identity(emit=lambda _ev: None))
    return brain, SETTINGS


def _types(j, **flt):
    return [e["type"] for e in j.query(limit=200, order="asc", **flt)["events"]]


def _ordered(types, required):
    it = iter(types)
    return all(any(r == t for t in it) for r in required)


async def test_reason_lifecycle_orders_and_never_stale_completes(tmp_path, journal, monkeypatch):
    brain, s = _brain(tmp_path)
    import autobot.brain.agent as agent_mod
    from autobot.brain.perception import Observation
    entered = asyncio.Event()
    release = asyncio.Event()

    async def ready_perceive(link, want_image=True):
        return Observation(telemetry={"ok": True, "connected": True, "awake": True})

    async def blocking_chat(self_, messages, tools=None):
        entered.set()
        await release.wait()
        raise AssertionError("provider should have been cancelled")

    monkeypatch.setattr(agent_mod, "perceive", ready_perceive, raising=False)
    monkeypatch.setattr(agent_mod, "vlm_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod, "omni_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod, "hybrid_enabled", lambda s=None: False, raising=False)
    monkeypatch.setattr(agent_mod.OpenAICompatibleClient, "chat", blocking_chat, raising=False)

    tick = asyncio.create_task(brain.tick(force=True))
    await asyncio.wait_for(entered.wait(), timeout=10.0)
    corr = f"reason-gen{brain._reason_gen}"
    await brain.emergency_stop("stop", master=True)
    res = await asyncio.wait_for(tick, timeout=5.0)
    assert res.get("cancelled") is True

    journal.flush_and_close()
    types = _types(journal, correlation_id=corr)
    assert _ordered(types, ["lock_wait_started", "started", "provider_wait_started", "cancelled"])
    assert "completed" not in types          # §4.1: a cancelled/superseded cycle NEVER emits completed


async def test_speech_lifecycle_orders(tmp_path, journal):
    from autobot.brain.speech import SpeechService
    from autobot.config import SETTINGS
    SETTINGS.update(talk_enabled=True)
    svc = SpeechService(MockRobotLink(), SETTINGS, emit=None)
    res = await svc.speak("hello rehearsal", check_say=False)
    assert res.get("ok") is not False
    journal.flush_and_close()
    types = _types(journal, category=obs.CAT_SPEECH)
    assert _ordered(types, ["requested", "render_started", "render_completed", "publish_started",
                            "publish_completed", "playback_started"])


def test_transport_lifecycle_orders_through_rtmnode(journal):
    from autobot.robot.rtm_node import RtmNode
    n = RtmNode(session_provider=lambda *a, **k: None)
    n.connected = True
    n._sidecar_instance_id = "SID"

    def feed(cmd):
        cid = cmd.get("command_id")
        if cid is not None:
            n._handle_event({"ev": "command_result", "command_id": cid, "cmd": cmd.get("cmd"),
                             "sidecar_instance_id": "SID", "sent_to_agora": True, "generation": 1, "epoch": 1})
        return True
    n._send = feed  # type: ignore[assignment]
    out = n.send_acked({"cmd": "drive", "ly": 0.2, "rx": 0.0, "dur": 0.2, "epoch": 1, "generation": 1,
                        "ticket_id": 1}, timeout=2.0)
    assert out["sent_to_agora"] is True
    journal.flush_and_close()
    types = _types(journal, category=obs.CAT_TRANSPORT)
    assert _ordered(types, ["queued_to_sidecar", "acknowledgement_received"])
    assert "command_result" in types
