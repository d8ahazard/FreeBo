"""P0 §8 — scripted unit tests for the hardware evidence harness (NO robot, NO network).

These prove the harness's HONESTY invariants with a fully mocked HTTP client:
  * evidence is never inferred from `ok` (absent facts are null);
  * the physical effect is never auto-set, and `--auto` can never be a PASS;
  * a dirty tree is diagnostics only;
  * a failed STOP and a non-reconciled RESUME ABORT the run.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "hardware_smoke", Path(__file__).resolve().parents[1] / "scripts" / "hardware_smoke.py")
hs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hs)  # type: ignore[union-attr]


class _Resp:
    def __init__(self, body: dict, status: int = 200):
        self._b = body
        self.status_code = status

    def json(self):
        return self._b


class _Client:
    """Scripted client: POST pops the next queued response for a path; GET returns a fixed status body."""

    def __init__(self, post_map: dict[str, list[dict]], status_body: dict | None = None):
        self.post_map = {k: list(v) for k, v in post_map.items()}
        self.status_body = status_body or {"readiness": {}}
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict | None = None):
        self.calls.append((url, json or {}))
        path = url.split("8200", 1)[-1] if "8200" in url else url
        for key, queue in self.post_map.items():
            if url.endswith(key):
                body = queue.pop(0) if queue else {"ok": True}
                return _Resp(body)
        return _Resp({"ok": True})

    def get(self, url: str):
        return _Resp(self.status_body)


def test_classify_never_infers_from_ok():
    # ok=True but none of the real fields present -> all evidence is null (not True), with reasons.
    c = hs.classify({"ok": True})
    assert c["queued_to_sidecar"] is None
    assert c["sdk_send_succeeded"] is None
    assert c["transport_dispatch_succeeded"] is None
    assert any("unknown" in r for r in c["reasons"])


def test_classify_reads_explicit_fields():
    c = hs.classify({"ok": True, "queued_to_sidecar": True, "sent_to_agora": False,
                     "local_inhibit_asserted": True, "transport_dispatch_succeeded": False})
    assert c["queued_to_sidecar"] is True
    assert c["sdk_send_succeeded"] is False
    assert c["local_inhibit_asserted"] is True
    assert c["transport_dispatch_succeeded"] is False


def test_auto_is_never_acceptance_eligible():
    assert hs.acceptance_eligible(auto=True, clean_tree=True) is False
    assert hs.acceptance_eligible(auto=False, clean_tree=False) is False   # dirty tree
    assert hs.acceptance_eligible(auto=False, clean_tree=True) is True


def test_auto_run_never_sets_effect_and_never_passes():
    # Under --auto the operator is never asked, so robot_effect_observed stays null and the gate can't pass.
    client = _Client({"/api/control": [], "/api/estop": [
        {"local_inhibit_asserted": True, "transport_dispatch_succeeded": True, "latched": True}],
        "/api/resume": [{"ok": True, "resumed": True}]})
    h = hs.Harness("http://127.0.0.1:8200", auto=True, client=client)
    row = h._master_stop("estop_0", "holding forward")
    assert row["robot_effect_observed"] is None              # never auto-set
    assert hs.estop_gate_pass(h.rows, eligible=False) is False


def test_failed_stop_aborts():
    # estop reports NO local inhibit / NO transport dispatch -> the run must abort.
    client = _Client({"/api/estop": [{"ok": False, "local_inhibit_asserted": False,
                                       "transport_dispatch_succeeded": False}]})
    h = hs.Harness("http://127.0.0.1:8200", auto=True, client=client)
    with pytest.raises(hs.HarnessAbort):
        h._master_stop("estop_fail", "holding forward")


def test_unreconciled_resume_aborts():
    client = _Client({"/api/resume": [{"ok": False, "error": "still inhibited"}]})
    h = hs.Harness("http://127.0.0.1:8200", auto=True, client=client)
    with pytest.raises(hs.HarnessAbort):
        h._resume("resume_0")


def test_classify_consumes_nested_transport_result():
    # agent_next_2 §8.1: the /api/estop response nests SDK facts under transport_result; classify reads them
    # WITHOUT substituting transport_dispatch_succeeded for the individual facts.
    api = {"ok": True, "local_inhibit_asserted": True, "transport_dispatch_succeeded": True,
           "transport_result": {"initial_zero_sdk_send_succeeded": True, "local_latch_set": True,
                                "retry_count": 3, "sent_to_agora": True, "dispatch_ts": 1.0, "completion_ts": 1.1}}
    c = hs.classify(api)
    assert c["initial_zero_sdk_send_succeeded"] is True
    assert c["local_sidecar_latch"] is True
    assert c["retry_count"] == 3
    assert c["sdk_send_succeeded"] is True
    assert c["local_inhibit_asserted"] is True
    assert c["sidecar_dispatch_ts"] == 1.0


def test_percentile_nearest_rank():
    assert hs.percentile([], 95) is None
    assert hs.percentile([100], 95) == 100
    assert hs.percentile([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 95) == 100
    assert hs.percentile([10, 20, 30, 40], 50) == 20


def test_gate_missing_measurement_is_fail():
    assert hs._gate(None, 600)["pass"] is False        # missing measurement is NOT a pass
    assert hs._gate(599, 600)["pass"] is True
    assert hs._gate(601, 600)["pass"] is False


def test_acceptance_report_fails_when_not_eligible_or_over_threshold():
    rows = [
        {"kind": "master_stop", "stop_latency_ms": 100, "robot_effect_observed": True,
         "post_stop_motion_observed": False},
        {"kind": "forward", "latency_ms": 100}, {"kind": "stale_effect", "rejected": True},
    ]
    assert hs.acceptance_report(rows, eligible=False)["pass"] is False      # never passes when ineligible
    rep = hs.acceptance_report(rows, eligible=True)
    assert rep["gates"]["stop_p95"]["pass"] is True
    # an over-threshold STOP fails the gate
    rows2 = [{"kind": "master_stop", "stop_latency_ms": 999, "robot_effect_observed": True,
              "post_stop_motion_observed": False}, {"kind": "forward", "latency_ms": 100},
             {"kind": "stale_effect", "rejected": True}]
    assert hs.acceptance_report(rows2, eligible=True)["gates"]["stop_p95"]["pass"] is False
    assert hs.acceptance_report(rows2, eligible=True)["pass"] is False


def test_estop_gate_pass_requires_observed_and_dispatched():
    rows = [
        {"kind": "master_stop", "robot_effect_observed": True,
         "classify": {"transport_dispatch_succeeded": True}},
        {"kind": "master_stop", "robot_effect_observed": None,    # operator unsure -> not a pass
         "classify": {"transport_dispatch_succeeded": True}},
    ]
    assert hs.estop_gate_pass(rows, eligible=True) is False
    rows[1]["robot_effect_observed"] = True
    assert hs.estop_gate_pass(rows, eligible=True) is True
    assert hs.estop_gate_pass(rows, eligible=False) is False      # eligibility still required
