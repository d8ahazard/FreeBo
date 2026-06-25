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
