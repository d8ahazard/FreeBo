"""P0-R4 item 10 — REAL sidecar child-process protocol tests.

Unlike test_rtm_node.py (which fakes `_send`), these spawn the actual Node `scripts/rtm_sidecar.js` with the
SDK send faked (AUTOBOT_RTM_FAKE=1) and drive it over stdin/stdout, exercising the genuine JS arbitration:
latch/generation, mandatory drive generation, stale rejection, honest E-STOP ack (incl. initial-zero send
failure), raw allowlist, and stop-wins-over-reset.

Skipped automatically if Node is not installed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SIDECAR = Path(__file__).resolve().parents[1] / "scripts" / "rtm_sidecar.js"
NODE = os.environ.get("AUTOBOT_NODE_BIN") or shutil.which("node")

pytestmark = pytest.mark.skipif(not NODE or not SIDECAR.is_file(), reason="node / sidecar not available")


class Sidecar:
    def __init__(self, fail: bool = False):
        env = {**os.environ, "AUTOBOT_RTM_FAKE": "1"}
        if fail:
            env["AUTOBOT_RTM_FAKE_FAIL"] = "1"
        self.p = subprocess.Popen([NODE, str(SIDECAR)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        self.ready = self._wait_for(lambda e: e.get("ev") == "ready", timeout=10)
        self.sid = self.ready.get("sidecar_instance_id")

    def send(self, **cmd) -> None:
        self.p.stdin.write(json.dumps(cmd) + "\n")
        self.p.stdin.flush()

    def unlatch(self, process_id: str = "P1") -> dict:
        """Bring a fresh (default-latched) sidecar to an unlatched epoch1/gen1 via the ONLY legal path: a
        reconcile (set_control, which cannot unlatch) followed by the two-phase release prepare_reset ->
        commit_reset (agent_next_2 §2)."""
        self.send(cmd="set_control", command_id=9001, process_instance_id=process_id,
                  epoch=0, generation=0, latched=True)
        self.result(9001)
        self.send(cmd="prepare_reset", command_id=9002, process_instance_id=process_id,
                  sidecar_instance_id=self.sid, expected_epoch=0, expected_generation=0,
                  release_epoch=1, release_generation=1)
        nonce = self.result(9002)["prepare_nonce"]
        self.send(cmd="commit_reset", command_id=9003, process_instance_id=process_id,
                  sidecar_instance_id=self.sid, prepare_nonce=nonce)
        return self.result(9003)

    def _wait_for(self, pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.p.stdout.readline()
            if not line:
                break
            try:
                ev = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if pred(ev):
                return ev
        raise AssertionError("sidecar did not produce the expected event in time")

    def result(self, command_id: int, timeout=5.0) -> dict:
        return self._wait_for(lambda e: e.get("ev") == "command_result" and e.get("command_id") == command_id,
                              timeout=timeout)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.p.kill()


@pytest.fixture
def sc():
    s = Sidecar()
    # Bring it to a known unlatched epoch1/gen1 via the two-phase release (set_control alone can never unlatch).
    res = s.unlatch()
    assert res["latched"] is False and res["reconciled"] is True
    yield s
    s.close()


def test_ready_announces_sidecar_instance_id():
    s = Sidecar()
    try:
        # the ready event captured by the constructor carried the id; re-prove via a command_result echo
        s.send(cmd="ping")
        s.send(cmd="set_control", command_id=99, epoch=1, generation=1, latched=False)
        r = s.result(99)
        assert isinstance(r.get("sidecar_instance_id"), str) and len(r["sidecar_instance_id"]) >= 8
    finally:
        s.close()


def test_stale_set_control_cannot_unlatch_after_stop(sc):
    sc.send(cmd="estop", command_id=20, epoch=2, generation=2)
    sc.result(20)
    # a stale set_control (epoch 1 < 2) must NOT unlatch
    sc.send(cmd="set_control", command_id=21, epoch=1, generation=1, latched=False)
    r = sc.result(21)
    assert r["latched"] is True and r.get("control_state_applied") is False


def test_unknown_raw_id_is_not_allowed_distinct_from_forbidden(sc):
    sc.send(cmd="raw", command_id=22, id=999999, data={})
    r = sc.result(22)
    assert r["ok"] is False and r["error"].startswith("raw_id_not_allowed")


def test_drive_without_generation_is_rejected(sc):
    sc.send(cmd="drive", command_id=2, ly=0.3, rx=0.0)   # no generation
    r = sc.result(2)
    assert r["sent_to_agora"] is False and r["error"] == "missing_generation"


def test_stale_generation_drive_is_rejected(sc):
    sc.send(cmd="drive", command_id=3, ly=0.3, rx=0.0, generation=0)   # current is 1
    r = sc.result(3)
    assert r["sent_to_agora"] is False and r["error"] == "stale_generation"


def test_matching_generation_drive_is_sent(sc):
    sc.send(cmd="drive", command_id=4, ly=0.2, rx=0.0, generation=1)
    r = sc.result(4)
    assert r["sent_to_agora"] is True and r.get("error") in (None, "")


def test_estop_reports_honest_initial_send(sc):
    sc.send(cmd="estop", command_id=5, generation=2)
    r = sc.result(5)
    assert r["local_latch_set"] is True
    assert r["initial_zero_sdk_send_succeeded"] is True
    assert r["latched"] is True and r["generation"] == 2 and r["retry_count"] == 3
    # a drive at the now-latched state is refused
    sc.send(cmd="drive", command_id=6, ly=0.2, rx=0.0, generation=2)
    assert sc.result(6)["error"] == "estop_latched"


def test_estop_initial_zero_send_failure_is_reported():
    s = Sidecar(fail=True)
    try:
        s.send(cmd="estop", command_id=1, generation=9)
        r = s.result(1)
        assert r["local_latch_set"] is True                       # local safety still asserted
        assert r["initial_zero_sdk_send_succeeded"] is False       # honest: transport failed
        assert r["ok"] is False
    finally:
        s.close()


def test_raw_movement_id_is_rejected(sc):
    # P0 §2.7: movement is in the IMMUTABLE hard-forbidden set (cannot travel raw, ever).
    sc.send(cmd="raw", command_id=7, id=101007, data={"ly": 50})
    r = sc.result(7)
    assert r["ok"] is False and r["error"].startswith("raw_id_hard_forbidden")


def test_prepare_reset_cannot_match_a_newer_stop(sc):
    # sc is unlatched at epoch1/gen1. A STOP advances to epoch2/gen2; a prepare that EXPECTS the old epoch1/gen1
    # must be rejected (stale_state) and the sidecar stays latched.
    sc.send(cmd="estop", command_id=8, epoch=2, generation=2)
    sc.result(8)
    sc.send(cmd="prepare_reset", command_id=9, process_instance_id="P1", sidecar_instance_id=sc.sid,
            expected_epoch=1, expected_generation=1, release_epoch=2, release_generation=2)
    r = sc.result(9)
    assert r["ok"] is False and r["error"] == "stale_state" and r["latched"] is True


def test_stop_after_prepare_invalidates_commit(sc):
    # Prepare a release from epoch1/gen1, then a STOP lands; the commit must be rejected and stay latched.
    sc.send(cmd="prepare_reset", command_id=30, process_instance_id="P1", sidecar_instance_id=sc.sid,
            expected_epoch=1, expected_generation=1, release_epoch=2, release_generation=2)
    nonce = sc.result(30)["prepare_nonce"]
    sc.send(cmd="estop", command_id=31, epoch=3, generation=3)   # STOP after prepare invalidates it
    sc.result(31)
    sc.send(cmd="commit_reset", command_id=32, process_instance_id="P1", sidecar_instance_id=sc.sid,
            prepare_nonce=nonce)
    r = sc.result(32)
    assert r["ok"] is False and r["latched"] is True


def test_two_phase_release_clears_latch_and_permits_drive(sc):
    sc.send(cmd="estop", command_id=10, epoch=3, generation=3)
    sc.result(10)
    sc.send(cmd="prepare_reset", command_id=11, process_instance_id="P1", sidecar_instance_id=sc.sid,
            expected_epoch=3, expected_generation=3, release_epoch=4, release_generation=4)
    nonce = sc.result(11)["prepare_nonce"]
    sc.send(cmd="commit_reset", command_id=12, process_instance_id="P1", sidecar_instance_id=sc.sid,
            prepare_nonce=nonce)
    r = sc.result(12)
    assert r["ok"] is True and r["latched"] is False and r["control_ready"] is True and r["generation"] == 4
    # drive at the new release generation/epoch now succeeds
    sc.send(cmd="drive", command_id=13, ly=0.2, rx=0.0, generation=4, epoch=4)
    assert sc.result(13)["sent_to_agora"] is True


def test_effect_requires_identity_and_ticket(sc):
    # agent_next_2 §4.5: a typed effect REQUIRES identity + a ticket (missing is rejected, not just stale).
    sc.send(cmd="dock", command_id=50)
    assert sc.result(50)["error"] == "missing_identity"
    sc.send(cmd="dock", command_id=51, process_instance_id="P1", sidecar_instance_id=sc.sid)
    assert sc.result(51)["error"] == "missing_ticket"
    sc.send(cmd="dock", command_id=52, process_instance_id="P1", sidecar_instance_id=sc.sid,
            epoch=1, generation=1, ticket_id=7)
    assert sc.result(52)["sent_to_agora"] is True


def test_effect_rejected_after_stop(sc):
    sc.send(cmd="estop", command_id=60, epoch=2, generation=2)
    sc.result(60)
    sc.send(cmd="laser", command_id=61, process_instance_id="P1", sidecar_instance_id=sc.sid,
            epoch=1, generation=1, ticket_id=1)
    assert sc.result(61)["error"] == "estop_latched"


def test_effect_stale_epoch_ticket_rejected(sc):
    # advance to a newer reconciled epoch via two-phase release, then a ticket for the OLD epoch is stale.
    sc.send(cmd="estop", command_id=70, epoch=5, generation=5)
    sc.result(70)
    sc.send(cmd="prepare_reset", command_id=71, process_instance_id="P1", sidecar_instance_id=sc.sid,
            expected_epoch=5, expected_generation=5, release_epoch=6, release_generation=6)
    nonce = sc.result(71)["prepare_nonce"]
    sc.send(cmd="commit_reset", command_id=72, process_instance_id="P1", sidecar_instance_id=sc.sid,
            prepare_nonce=nonce)
    sc.result(72)
    sc.send(cmd="laser", command_id=73, process_instance_id="P1", sidecar_instance_id=sc.sid,
            epoch=1, generation=6, ticket_id=2)        # epoch 1 is stale (now 6)
    assert sc.result(73)["error"] == "stale_epoch"


def test_parent_death_latches_and_new_instance_starts_latched():
    # agent_next_2 §2.5: closing the parent pipe after an unlatched release must fail-safe (latch + zero + exit);
    # a brand-new sidecar instance then starts LATCHED and refuses effects until a full new reconciliation.
    s = Sidecar()
    s.unlatch()                                   # unlatched at epoch1/gen1
    s.p.stdin.close()                             # parent pipe end -> fail-safe shutdown
    assert s.p.wait(timeout=5) == 0               # clean exit
    s2 = Sidecar()
    try:
        assert s2.sid and s2.sid != s.sid         # a different (replacement) instance
        s2.send(cmd="drive", command_id=1, generation=0, ly=0.2)
        assert s2.result(1)["sent_to_agora"] is False   # fresh instance is latched; drive refused
    finally:
        s2.close()


def test_reused_prepare_nonce_is_rejected(sc):
    sc.send(cmd="estop", command_id=40, epoch=3, generation=3)
    sc.result(40)
    sc.send(cmd="prepare_reset", command_id=41, process_instance_id="P1", sidecar_instance_id=sc.sid,
            expected_epoch=3, expected_generation=3, release_epoch=4, release_generation=4)
    nonce = sc.result(41)["prepare_nonce"]
    sc.send(cmd="commit_reset", command_id=42, process_instance_id="P1", sidecar_instance_id=sc.sid,
            prepare_nonce=nonce)
    assert sc.result(42)["ok"] is True
    # the consumed nonce cannot commit a second release
    sc.send(cmd="commit_reset", command_id=43, process_instance_id="P1", sidecar_instance_id=sc.sid,
            prepare_nonce=nonce)
    assert sc.result(43)["ok"] is False
