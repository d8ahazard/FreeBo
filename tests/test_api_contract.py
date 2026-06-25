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
