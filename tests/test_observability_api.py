"""agent_next_3 Gate C — observability instrumentation + query API + export (mock link, no network)."""
from __future__ import annotations

import os

import pytest

from autobot import observability as obs


class _ReqQ:
    def __init__(self, params):
        self.query_params = params


@pytest.fixture(scope="module")
def server():
    os.environ["AUTOBOT_ROBOT_LINK"] = "mock"
    from autobot.web import server as srv
    return srv


def _fresh_journal(tmp_path):
    obs.configure(str(tmp_path / "events.jsonl"), max_mem=2000)


def _clear(server):
    server.brain.safety.arb._unsafe_clear_for_tests()
    server.brain._stopped = False


async def test_estop_emits_correlated_safety_transition(server, tmp_path):
    _fresh_journal(tmp_path)
    _clear(server)
    await server.api_estop()
    j = obs.journal()
    rows = j.query(category=obs.CAT_SAFETY_TRANSITION, type="master_stop")["events"]
    assert rows, "a master_stop safety.transition event must be emitted"
    ev = rows[-1]
    assert ev["requested"] == "stop" and ev["effective"] == "inhibited"
    assert ev["correlation_id"] and ev["correlation_id"].startswith("stop-gen")
    _clear(server)


async def test_effect_admission_emits_event(server, tmp_path):
    _fresh_journal(tmp_path)
    _clear(server)
    t = server.brain.safety.admit_effect("dock", "ai", server.SETTINGS.snapshot())
    assert t is not None
    rows = obs.journal().query(category=obs.CAT_EFFECT, type="dock")["events"]
    assert rows and rows[-1]["effective"] == "admitted" and rows[-1]["ticket_id"] is not None
    _clear(server)


async def test_query_endpoint_filters(server, tmp_path):
    _fresh_journal(tmp_path)
    j = obs.journal()
    j.emit(obs.CAT_MOTION, "drive", "ai", outcome="moved", correlation_id="zz")
    j.emit(obs.CAT_MOTION, "turn", "ai", outcome="blocked", correlation_id="zz")
    resp = await server.api_events(_ReqQ({"category": obs.CAT_MOTION, "outcome": "moved"}))
    import json
    body = json.loads(resp.body.decode())
    assert body["returned"] == 1 and body["events"][0]["type"] == "drive"
    # correlation trace endpoint groups the incident
    tr = await server.api_events_trace("zz")
    assert len(json.loads(tr.body.decode())["events"]) == 2


async def test_export_bundle_is_redacted_with_manifest(server, tmp_path):
    _fresh_journal(tmp_path)
    j = obs.journal()
    j.emit(obs.CAT_MOTION, "drive", "ai", correlation_id="inc1", detail={"api_key": "sk-XXX", "ly": 0.2})
    import json
    resp = await server.api_events_export(correlation_id="inc1")
    bundle = json.loads(resp.body.decode())
    assert bundle["schema"] == "freebo.incident.v1"
    assert bundle["software_sha"] and "platform" in bundle and "readiness" in bundle
    assert bundle["event_count"] == 1
    blob = json.dumps(bundle)
    assert "sk-XXX" not in blob and "<redacted>" in blob   # secret redacted at the journal boundary


async def test_summary_endpoint(server, tmp_path):
    _fresh_journal(tmp_path)
    j = obs.journal()
    for ms in (10, 50):
        j.emit(obs.CAT_MOTION, "drive", "ai", outcome="moved", latency_ms=ms)
    import json
    resp = await server.api_events_summary(since_seq=0)
    s = json.loads(resp.body.decode())
    assert s["total"] >= 2 and s["by_category"].get(obs.CAT_MOTION, 0) >= 2
