"""agent_next_3 Gate C — observability instrumentation + query API + export (mock link, no network)."""
from __future__ import annotations

import os

import pytest

from autobot import observability as obs


class _Client:
    def __init__(self, host="127.0.0.1"):
        self.host = host


class _ReqQ:
    def __init__(self, params=None, host="127.0.0.1", headers=None):
        self.query_params = params or {}
        self.client = _Client(host)
        self.headers = headers or {}


@pytest.fixture(scope="module")
def server():
    os.environ["AUTOBOT_ROBOT_LINK"] = "mock"
    from autobot.web import server as srv
    # agent_next_5 §1.5: the access policy keys on the CONFIGURED bind. Default a loopback bind for the endpoint
    # tests (a loopback-only deployment is allowed without a token); access-policy tests override the bind host.
    # `host` is a deploy setting (not USER_EDITABLE), so set the attribute directly.
    srv.SETTINGS.host = "127.0.0.1"
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
    tr = await server.api_events_trace(_ReqQ(), "zz")
    assert len(json.loads(tr.body.decode())["events"]) == 2


async def test_query_endpoint_rejects_bad_int_param(server, tmp_path):
    _fresh_journal(tmp_path)
    resp = await server.api_events(_ReqQ({"limit": "notanint"}))
    assert resp.status_code == 400


async def test_query_endpoint_rejects_malformed_cursor(server, tmp_path):
    _fresh_journal(tmp_path)
    resp = await server.api_events(_ReqQ({"cursor": "!!!garbage!!!"}))
    assert resp.status_code == 400


async def test_non_loopback_bind_requires_owner_token(server, tmp_path, monkeypatch):
    _fresh_journal(tmp_path)
    monkeypatch.delenv("AUTOBOT_OWNER_TOKEN", raising=False)
    monkeypatch.setattr(server.SETTINGS, "host", "0.0.0.0")   # non-loopback bind -> fail closed
    # no token configured -> forbidden, regardless of (loopback) peer
    assert (await server.api_events(_ReqQ())).status_code == 403
    # token configured but wrong/absent header -> 401
    monkeypatch.setenv("AUTOBOT_OWNER_TOKEN", "sekret")
    assert (await server.api_events(_ReqQ())).status_code == 401
    # correct header -> allowed
    assert (await server.api_events(_ReqQ(headers={"X-Owner-Token": "sekret"}))).status_code == 200


async def test_reverse_proxy_loopback_peer_cannot_bypass(server, tmp_path, monkeypatch):
    # §1.5: a request that APPEARS to come from 127.0.0.1 (forwarded by a local reverse proxy) must NOT bypass
    # the token when the configured bind is non-loopback. The decision ignores the peer + X-Forwarded-For.
    _fresh_journal(tmp_path)
    monkeypatch.delenv("AUTOBOT_OWNER_TOKEN", raising=False)
    monkeypatch.setattr(server.SETTINGS, "host", "0.0.0.0")
    spoof = _ReqQ(host="127.0.0.1", headers={"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"})
    assert (await server.api_events(spoof)).status_code == 403       # loopback peer is NOT trusted
    monkeypatch.setenv("AUTOBOT_OWNER_TOKEN", "sekret")
    assert (await server.api_events(spoof)).status_code == 401       # still needs the real token
    ok = _ReqQ(host="127.0.0.1", headers={"X-Owner-Token": "sekret"})
    assert (await server.api_events(ok)).status_code == 200


async def test_loopback_bind_allows_without_token(server, tmp_path, monkeypatch):
    _fresh_journal(tmp_path)
    monkeypatch.delenv("AUTOBOT_OWNER_TOKEN", raising=False)
    monkeypatch.setattr(server.SETTINGS, "host", "127.0.0.1")    # loopback-only deployment -> allowed
    assert (await server.api_events(_ReqQ())).status_code == 200


async def test_hardware_gate_states_hardware_not_run(server, tmp_path):
    _fresh_journal(tmp_path)
    import json
    resp = await server.api_hardware_gate(_ReqQ())
    body = json.loads(resp.body.decode())
    assert body["hardware_run"] is False and body["physical_acceptance"] is False


async def test_export_bundle_is_redacted_with_manifest(server, tmp_path):
    _fresh_journal(tmp_path)
    j = obs.journal()
    j.emit(obs.CAT_MOTION, "drive", "ai", correlation_id="inc1", detail={"api_key": "sk-XXX", "ly": 0.2})
    import json
    resp = await server.api_events_export(_ReqQ(), correlation_id="inc1")
    bundle = json.loads(resp.body.decode())
    assert bundle["schema"] == "freebo.incident" and bundle["schema_version"] == 2
    assert bundle["software_sha"] and "platform" in bundle and "readiness" in bundle
    assert bundle["hardware_run"] is False
    assert "attachment" in resp.headers.get("content-disposition", "")
    blob = json.dumps(bundle)
    assert "sk-XXX" not in blob and "<redacted>" in blob   # secret redacted at the journal boundary


async def test_summary_endpoint(server, tmp_path):
    _fresh_journal(tmp_path)
    j = obs.journal()
    for ms in (10, 50):
        j.emit(obs.CAT_MOTION, "drive", "ai", outcome="moved", latency_ms=ms)
    import json
    resp = await server.api_events_summary(_ReqQ())
    s = json.loads(resp.body.decode())
    assert s["total"] >= 2 and s["by_category"].get(obs.CAT_MOTION, 0) >= 2
