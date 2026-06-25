"""agent_next_5 §6 — scripted unit tests for the supervised R4.0 harness (NO robot, NO network).

Prove the honesty + safety invariants with a fully simulated client + prompter:
  * evidence is never inferred from `ok` (absent facts are null);
  * arming refuses under --auto / wrong SHA / dirty tree / desync / unhealthy journal; no motion before arming;
  * deterministic scenario counts; acceptance fails on any missing measurement;
  * abort-on-unexpected-motion issues a priority E-STOP; a stale/latched effect must be rejected.
"""
from __future__ import annotations

import importlib.util
import threading
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


class _SimClient:
    """Models the server's latch state so the stale-effect probe + STOP/RESUME behave realistically."""

    def __init__(self, *, sha="SHA1", variant="AIR2", synchronized=True, journal_alive=True,
                 estop_dispatches=True, video_age=0.1):
        self.latched = False
        self.calls: list[tuple[str, dict]] = []
        self._cmd = 0
        self._lock = threading.Lock()          # mover thread + main thread share this client
        self.sha, self.variant, self.synchronized = sha, variant, synchronized
        self.journal_alive, self.estop_dispatches, self.video_age = journal_alive, estop_dispatches, video_age

    def _readiness(self):
        return {"synchronized": self.synchronized, "video_age": self.video_age, "telemetry_age": 0.1}

    def get(self, url, headers=None):
        if url.endswith("/api/hardware_gate"):
            return _Resp({"software_sha": self.sha, "hardware_run": False,
                          "journal_health": {"writer_alive": self.journal_alive, "persist_ok": True},
                          "readiness": self._readiness()})
        if url.endswith("/api/events/health"):
            return _Resp({"writer_alive": self.journal_alive, "persist_ok": True})
        if url.endswith("/api/state") or url.endswith("/api/status"):
            return _Resp({"settings": {"robot_variant": self.variant},
                          "brain": {"readiness": self._readiness()}})
        return _Resp({})

    def post(self, url, json=None, headers=None):
        with self._lock:
            self.calls.append((url, json or {}))
            if url.endswith("/api/estop"):
                self.latched = True
                return _Resp({"local_inhibit_asserted": True,
                              "transport_dispatch_succeeded": self.estop_dispatches, "latched": True,
                              "transport_result": {"initial_zero_sdk_send_succeeded": self.estop_dispatches,
                                                   "retry_count": 3, "sent_to_agora": self.estop_dispatches,
                                                   "dispatch_ts": 1.0, "completion_ts": 1.1}})
            if url.endswith("/api/resume"):
                self.latched = False
                return _Resp({"ok": True, "resumed": True, "reconciled": True})
            if url.endswith("/api/control"):
                kind = (json or {}).get("kind")
                if kind in ("move", "drive"):
                    if self.latched:
                        return _Resp({"ok": False, "blocked": "motion not admitted (STOP/latched)"})
                    self._cmd += 1
                    return _Resp({"ok": True, "sent_to_agora": True, "command_id": self._cmd})
                return _Resp({"ok": True})
            return _Resp({"ok": True})

    def move_calls(self):
        with self._lock:
            return [c for c in self.calls if c[0].endswith("/api/control") and c[1].get("kind") in ("move", "drive")]


def _good_prompt(q: str) -> str:
    ql = q.lower()
    if "presence phrase" in ql or "type the presence" in ql:
        return hs.PRESENCE_PHRASE
    if "after the stop" in ql or "unexpected" in ql:
        return "n"
    return "y"


def _harness(client, *, auto=False, armed_flag=True, expect_sha="SHA1", prompter=_good_prompt):
    return hs.Harness("http://127.0.0.1:8200", auto=auto, armed_flag=armed_flag, expect_sha=expect_sha,
                      client=client, prompter=prompter)


# ---- classify / caps (pure) ----
def test_classify_never_infers_from_ok():
    c = hs.classify({"ok": True})
    assert c["queued_to_sidecar"] is None and c["sdk_send_succeeded"] is None
    assert c["transport_dispatch_succeeded"] is None and any("unknown" in r for r in c["reasons"])


def test_classify_consumes_nested_transport_result():
    c = hs.classify({"ok": True, "local_inhibit_asserted": True, "transport_dispatch_succeeded": True,
                     "transport_result": {"initial_zero_sdk_send_succeeded": True, "retry_count": 3,
                                          "sent_to_agora": True, "dispatch_ts": 1.0, "completion_ts": 1.1}})
    assert c["initial_zero_sdk_send_succeeded"] is True and c["retry_count"] == 3
    assert c["sdk_send_succeeded"] is True and c["sidecar_dispatch_ts"] == 1.0


def test_clamp_caps():
    # forward cap raised to 0.30 (above the Air 2 forward deadband 0.25); turn stays 0.18, duration 0.60.
    assert hs.clamp_caps(0.9, 0.9, 5.0) == (0.30, 0.18, 0.60)
    assert hs.clamp_caps(-0.9, -0.9, -1) == (-0.30, -0.18, 0.0)
    assert hs.clamp_caps(0.1, 0.1, 0.3) == (0.1, 0.1, 0.3)
    assert hs.R4_0_CAPS["forward_mag"] == 0.30 and hs.R4_0_CAPS["forward_mag"] > 0.25   # above the deadband


def test_percentile_and_gate():
    assert hs.percentile([], 95) is None and hs.percentile([10, 20, 30, 40], 50) == 20
    assert hs._gate(None, 600)["pass"] is False                      # missing measurement is NOT a pass
    assert hs._gate(599, 600)["pass"] is True and hs._gate(601, 600)["pass"] is False


# ---- §3.1 arming ----
def _state(variant="AIR2", synchronized=True):
    return {"settings": {"robot_variant": variant}, "brain": {"readiness": {"synchronized": synchronized}}}


def _gate(sha="SHA1", alive=True):
    return {"software_sha": sha, "journal_health": {"writer_alive": alive, "persist_ok": True}}


def test_arming_never_under_auto():
    c = hs.arming_conditions(_state(), _gate(), expect_sha="SHA1", auto=True, armed_flag=True,
                             presence_ok=True, checklist_ok=True)
    assert c["armed_ok"] is False


def test_arming_requires_sha_match():
    c = hs.arming_conditions(_state(), _gate(sha="OTHER"), expect_sha="SHA1", auto=False, armed_flag=True,
                             presence_ok=True, checklist_ok=True)
    assert c["app_sha_matches"] is False and c["armed_ok"] is False


def test_arming_requires_synchronized_and_air2_and_journal():
    desync = hs.arming_conditions(_state(synchronized=False), _gate(), expect_sha="SHA1", auto=False,
                                  armed_flag=True, presence_ok=True, checklist_ok=True)
    assert desync["control_synchronized"] is False and desync["armed_ok"] is False
    notair2 = hs.arming_conditions(_state(variant="MOCK"), _gate(), expect_sha="SHA1", auto=False,
                                   armed_flag=True, presence_ok=True, checklist_ok=True)
    assert notair2["live_air2_link"] is False and notair2["armed_ok"] is False
    badj = hs.arming_conditions(_state(), _gate(alive=False), expect_sha="SHA1", auto=False, armed_flag=True,
                                presence_ok=True, checklist_ok=True)
    assert badj["journal_writer_healthy"] is False and badj["armed_ok"] is False


def test_arming_requires_presence_and_checklist():
    no_phrase = hs.arming_conditions(_state(), _gate(), expect_sha="SHA1", auto=False, armed_flag=True,
                                     presence_ok=False, checklist_ok=True)
    assert no_phrase["armed_ok"] is False
    no_check = hs.arming_conditions(_state(), _gate(), expect_sha="SHA1", auto=False, armed_flag=True,
                                    presence_ok=True, checklist_ok=False)
    assert no_check["armed_ok"] is False


def test_auto_is_never_eligible():
    assert hs.acceptance_eligible(auto=True, clean_tree=True, armed=True) is False
    assert hs.acceptance_eligible(auto=False, clean_tree=False, armed=True) is False
    assert hs.acceptance_eligible(auto=False, clean_tree=True, armed=False) is False


# ---- no motion before arming ----
def test_no_motion_before_arming():
    client = _SimClient()
    h = _harness(client)
    h.armed = False                                                  # arming did not complete
    with pytest.raises(hs.HarnessAbort):
        h.run_r4_0()
    assert client.move_calls() == []                                # ZERO motion issued before arming


def test_auto_run_never_arms_and_issues_no_motion(monkeypatch):
    monkeypatch.setattr(hs, "tree_is_clean", lambda: True)
    client = _SimClient()
    h = _harness(client, auto=True)
    assert h.arm() is False                                          # --auto never arms
    assert client.move_calls() == []


# ---- abort behavior ----
def test_abort_on_unexpected_motion_issues_priority_estop(monkeypatch):
    monkeypatch.setattr(hs, "tree_is_clean", lambda: True)

    def prompt(q):
        ql = q.lower()
        if "presence phrase" in ql or "type the presence" in ql:
            return hs.PRESENCE_PHRASE
        if "unexpected" in ql:
            return "y"                                              # operator reports UNEXPECTED motion
        if "after the stop" in ql:
            return "n"
        return "y"
    client = _SimClient()
    h = _harness(client, prompter=prompt)
    assert h.arm() is True
    with pytest.raises(hs.HarnessAbort):
        h.run_r4_0()
    assert any(u.endswith("/api/estop") for u, _ in client.calls)   # priority E-STOP was issued on abort


def test_failed_stop_aborts(monkeypatch):
    monkeypatch.setattr(hs, "tree_is_clean", lambda: True)
    client = _SimClient(estop_dispatches=False)                     # STOP transport does not dispatch
    h = _harness(client)
    assert h.arm() is True
    with pytest.raises(hs.HarnessAbort):
        h.run_r4_0()


# ---- acceptance report ----
def test_acceptance_report_missing_measurement_fails():
    # missing required counts -> fail; missing halt observation on a STOP -> fail.
    rows = [{"kind": "master_stop", "latency_ms": 100, "latched": True,
             "classify": {"local_inhibit_asserted": True, "transport_dispatch_succeeded": True},
             "observations": {"halt_observed": None, "post_stop_motion_observed": False,
                              "unexpected_motion_observed": False}}]
    rep = hs.acceptance_report(rows, eligible=True)
    assert rep["counts_ok"] is False                                # nowhere near the required trial counts
    assert rep["stop_gates"]["every_stop_halt_observed"] is False   # unknown halt is not a pass
    assert rep["pass"] is False
    assert hs.acceptance_report(rows, eligible=False)["pass"] is False


def test_full_scripted_run_produces_counts_and_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(hs, "tree_is_clean", lambda: True)
    monkeypatch.setattr(hs, "commit_sha", lambda: "SHA1")
    monkeypatch.setattr(hs, "EVID", tmp_path)
    client = _SimClient()
    h = _harness(client)
    h._stop_settle = 0.02                                    # don't sleep 1.2s × 10 in the test
    assert h.arm() is True
    h.run_r4_0()
    manifest = h.save()
    counts = manifest["acceptance_report"]["counts"]
    assert counts["eyes"] >= 5 and counts["forward"] >= 5 and counts["turn"] >= 5
    assert counts["master_stop"] >= 10
    assert any(r["kind"] == "stale_effect" and r["rejected"] is True for r in h.rows)  # stale effect rejected
    assert manifest["acceptance_report"]["pass"] is True
    assert manifest["verdict"] == "PASS"
    # evidence written under the SHA-scoped immutable directory
    assert (tmp_path / "hardware" / "SHA1" / "r4_0").exists()


def test_freshness_gate_refuses_stale_feeds():
    assert hs._freshness_ok({"video_age": 0.1, "telemetry_age": 0.1}) is True
    assert hs._freshness_ok({"video_age": 9.0}) is False


def test_sustained_mover_drives_then_self_stops_on_latch():
    # The mover keeps issuing capped pulses (so the robot is moving at STOP time), and self-stops the instant a
    # pulse is refused (the STOP latched the robot).
    client = _SimClient()
    h = _harness(client)
    mover = hs.SustainedMover(h._mover_post, 0.30, 0.0, pulse_s=0.5, gap_s=0.02).start()
    import time as _t
    _t.sleep(0.15)
    assert mover.pulses >= 1                                 # the robot was actually being driven
    client.latched = True                                   # simulate a STOP landing
    _t.sleep(0.1)
    mover.stop()
    assert mover.refused is True                             # mover saw the refusal and stopped issuing motion


def test_master_stop_trial_has_motion_in_flight(monkeypatch):
    # The STOP-during-motion staging must put a move in flight before the master STOP (the agent_next_5 fix).
    monkeypatch.setattr(hs, "tree_is_clean", lambda: True)
    client = _SimClient()
    h = _harness(client)
    mover = h._start_scenario_motion("forward_pulse")
    import time as _t
    _t.sleep(0.15)
    try:
        assert mover.pulses >= 1 and client.move_calls()    # motion underway before the STOP
    finally:
        mover.stop()
