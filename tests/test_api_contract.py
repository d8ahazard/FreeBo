"""agent_next_2 §11 — backend API contract tests (mock link).

While master-inhibited, reasoning endpoints return a non-2xx inhibition result and mutate no transcript/history;
the STOP response preserves nested transport evidence; RESUME is admission-gated.
"""
from __future__ import annotations

import os

import pytest


class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


@pytest.fixture(scope="module")
def server():
    os.environ["AUTOBOT_ROBOT_LINK"] = "mock"
    from autobot.web import server as srv
    return srv


def _clear(server):
    # test-only: drop any master inhibit/latch between cases
    server.brain.safety.arb._unsafe_clear_for_tests()
    server.brain._stopped = False


async def test_chat_while_inhibited_returns_423_and_mutates_nothing(server):
    _clear(server)
    server.brain.safety.begin_master_stop()                 # assert master inhibit
    before = len(server.brain.buffer.transcripts)
    resp = await server.api_chat(_Req({"text": "hello robot"}))
    assert resp.status_code == 423
    assert len(server.brain.buffer.transcripts) == before    # no transcript mutation
    _clear(server)


async def test_tick_while_inhibited_returns_423(server):
    _clear(server)
    server.brain.safety.begin_master_stop()
    resp = await server.api_tick()
    assert resp.status_code == 423
    _clear(server)


async def test_estop_response_preserves_nested_transport_evidence(server):
    _clear(server)
    resp = await server.api_estop()
    import json
    body = json.loads(resp.body.decode())
    assert "transport_result" in body                        # nested evidence preserved (not just a boolean)
    assert "local_inhibit_asserted" in body and "transport_dispatch_succeeded" in body
    assert server.brain.safety.is_master_inhibited() is True
    _clear(server)


async def test_resume_not_admissible_returns_409_when_not_stopped(server):
    _clear(server)                                           # not latched/inhibited
    resp = await server.api_resume()
    assert resp.status_code == 409                           # reset not admissible
    import json
    assert json.loads(resp.body.decode()).get("ok") is False


class _FakeRtm:
    def __init__(self):
        self.sent = []
        self._process_instance_id = "P"
        self._sidecar_instance_id = "S"

    def send_acked(self, cmd, timeout=1.5):
        self.sent.append(cmd)
        return {"ok": True, "sent_to_agora": True, "command_id": 1, **cmd}


async def test_manual_air2_motion_reaches_sidecar_with_full_ticket(server, monkeypatch):
    # agent_next_5 §1.1: manual /api/control motion through the Air 2 link must reach the (fake) sidecar carrying
    # the COMPLETE ticket — epoch + generation + ticket_id + effect_class=motion. A partial ticket fails closed.
    _clear(server)
    from autobot.robot.air2_native_link import Air2NativeLink
    link = Air2NativeLink.__new__(Air2NativeLink)            # skip heavy __init__; we only exercise drive/move
    fake = _FakeRtm()
    link.rtm = fake
    server.SETTINGS.update(setup_complete=True, asleep=False, allow_motion=True, max_speed=1.0)
    monkeypatch.setattr(server, "LINK", link, raising=True)

    resp = await server.api_control(_Req({"kind": "drive", "ly": 0.2, "rx": 0.0}))
    import json
    body = json.loads(resp.body.decode())
    assert body.get("sent_to_agora") is True
    assert fake.sent, "manual drive never reached the sidecar"
    cmd = fake.sent[-1]
    assert cmd["cmd"] == "drive" and cmd["effect_class"] == "motion"
    for k in ("epoch", "generation", "ticket_id"):
        assert cmd.get(k) is not None, f"manual motion dropped {k}"

    fake.sent.clear()
    resp2 = await server.api_control(_Req({"kind": "move", "ly": 0.2, "rx": 0.0, "duration": 0.3}))
    cmd2 = fake.sent[-1]
    assert cmd2.get("ticket_id") is not None and cmd2.get("epoch") is not None and cmd2.get("generation") is not None
    _clear(server)


async def test_air2_motion_without_ticket_fails_closed():
    # The Air 2 link itself rejects a partial ticket (no fallback) — defense in depth behind the server fix.
    from autobot.robot.air2_native_link import Air2NativeLink
    link = Air2NativeLink.__new__(Air2NativeLink)
    link.rtm = _FakeRtm()
    res = await link.drive(0.2, 0.0, generation=1, epoch=1)   # ticket_id missing
    assert res.get("error") == "missing_motion_ticket" and res.get("sent_to_agora") is False
    assert not link.rtm.sent                                  # nothing reached the sidecar
