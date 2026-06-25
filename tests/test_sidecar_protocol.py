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
        self._wait_for(lambda e: e.get("ev") == "ready", timeout=10)

    def send(self, **cmd) -> None:
        self.p.stdin.write(json.dumps(cmd) + "\n")
        self.p.stdin.flush()

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
    # Reconcile to a known unlatched state at generation 1 (sidecar boots default-safe latched=true).
    s.send(cmd="set_control", command_id=1, generation=1, latched=False)
    s.result(1)
    yield s
    s.close()


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
    sc.send(cmd="raw", command_id=7, id=101007, data={"ly": 50})
    r = sc.result(7)
    assert r["ok"] is False and r["error"].startswith("raw_id_not_allowed")


def test_reset_cannot_clear_a_newer_stop(sc):
    # STOP advances generation to 5; a reset that EXPECTED gen 1 must be rejected and stay latched.
    sc.send(cmd="estop", command_id=8, generation=5)
    sc.result(8)
    sc.send(cmd="estop_reset", command_id=9, expected_generation=1, generation=1)
    r = sc.result(9)
    assert r["ok"] is False and r["error"] == "stale_reset_generation" and r["latched"] is True


def test_matching_reset_clears_latch_and_reports_control_ready(sc):
    sc.send(cmd="estop", command_id=10, generation=3)
    sc.result(10)
    sc.send(cmd="estop_reset", command_id=11, expected_generation=3, generation=3)
    r = sc.result(11)
    assert r["ok"] is True and r["latched"] is False and r["control_ready"] is True and r["generation"] == 3
    # drive at the reset generation now succeeds
    sc.send(cmd="drive", command_id=12, ly=0.2, rx=0.0, generation=3)
    assert sc.result(12)["sent_to_agora"] is True
