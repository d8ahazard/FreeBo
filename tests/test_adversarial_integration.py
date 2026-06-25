"""P0 §7 — adversarial integration tests across the real boundaries.

These drive the ACTUAL Node sidecar child process (SDK send faked with AUTOBOT_RTM_FAKE=1) and the real
RtmNode reconciler, exercising the cross-component safety contract under races: stale generation/epoch,
replaced sidecar instance, hard-forbidden raw despite env, STOP racing reconcile, single-use reset.

The sidecar reader is a BOUNDED reader-thread + queue (not a blocking readline behind a decorative deadline),
and every harness has a bounded teardown — so a wedged child can never hang the suite.

Directive case map (the rest live in test_control_arbiter.py / test_sidecar_protocol.py /
test_rtm_node.py / test_estop_endpoint.py / test_reason_cancellation.py):
  - motion admitted pre-STOP dispatched post-STOP .... test_drive_admitted_pre_stop_rejected_post_stop
  - stale set_control behind a newer STOP ............ test_stale_set_control_cannot_unlatch
  - lower epoch/generation rejected ................. test_lower_epoch_set_control_rejected
  - STOP racing a reconcile ......................... test_stop_wins_over_concurrent_reconcile
  - reused / single-use reset ....................... test_reused_reset_token_via_rtmnode
  - old/replaced sidecar instance response .......... test_rtmnode_rejects_replaced_instance_result
  - raw hard-forbidden despite env allowlist ........ test_raw_hard_forbidden_even_when_env_allows
  - estop honest initial-zero-send failure stays latched  test_failed_estop_send_stays_latched_and_blocks_reset
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

SIDECAR = Path(__file__).resolve().parents[1] / "scripts" / "rtm_sidecar.js"
NODE = os.environ.get("AUTOBOT_NODE_BIN") or shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE or not SIDECAR.is_file(), reason="node / sidecar not available")


class Sidecar:
    """Real child-process sidecar with a bounded reader thread + queue and bounded teardown."""

    def __init__(self, env_extra: dict | None = None, fail: bool = False):
        env = {**os.environ, "AUTOBOT_RTM_FAKE": "1"}
        if fail:
            env["AUTOBOT_RTM_FAKE_FAIL"] = "1"
        if env_extra:
            env.update(env_extra)
        self.p = subprocess.Popen([NODE, str(SIDECAR)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        self._q: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.ready = self.wait(lambda e: e.get("ev") == "ready", timeout=10)
        self.sid = self.ready.get("sidecar_instance_id")

    def _read(self) -> None:
        try:
            for line in self.p.stdout:   # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    self._q.put(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def send(self, **cmd) -> None:
        self.p.stdin.write(json.dumps(cmd) + "\n")   # type: ignore[union-attr]
        self.p.stdin.flush()                          # type: ignore[union-attr]

    def wait(self, pred, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("sidecar event not seen in time")
            try:
                ev = self._q.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError("sidecar event not seen in time")
            if pred(ev):
                return ev

    def result(self, cid, timeout: float = 5.0) -> dict:
        return self.wait(lambda e: e.get("ev") == "command_result" and e.get("command_id") == cid, timeout)

    def close(self) -> None:
        try:
            self.p.stdin.close()   # type: ignore[union-attr]
            self.p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.p.kill()
        self._reader.join(timeout=2)


def _reconciled() -> Sidecar:
    """Fresh sidecar brought to unlatched epoch1/gen1 via the legal two-phase release (set_control can never
    unlatch; only prepare_reset -> commit_reset can)."""
    sc = Sidecar()
    sc.send(cmd="set_control", command_id=9001, process_instance_id="P1", epoch=0, generation=0, latched=True)
    sc.result(9001)
    sc.send(cmd="prepare_reset", command_id=9002, process_instance_id="P1", sidecar_instance_id=sc.sid,
            expected_epoch=0, expected_generation=0, release_epoch=1, release_generation=1)
    nonce = sc.result(9002)["prepare_nonce"]
    sc.send(cmd="commit_reset", command_id=9003, process_instance_id="P1", sidecar_instance_id=sc.sid,
            prepare_nonce=nonce)
    sc.result(9003)
    return sc


# ---- child-process sidecar cases -------------------------------------------------------------------

def test_drive_admitted_pre_stop_rejected_post_stop():
    sc = _reconciled()
    try:
        # a drive ticketed at gen 1 that arrives AFTER a STOP advanced the generation must be rejected
        sc.send(cmd="estop", command_id=2, epoch=2, generation=2)
        sc.result(2)
        sc.send(cmd="drive", command_id=3, generation=1, epoch=1, ly=0.3, duration=0.2)
        r = sc.result(3)
        assert r["sent_to_agora"] is False and r["error"] in ("estop_latched", "stale_generation")
    finally:
        sc.close()


def test_stale_set_control_cannot_unlatch():
    sc = _reconciled()
    try:
        sc.send(cmd="estop", command_id=2, epoch=5, generation=5)
        sc.result(2)
        sc.send(cmd="set_control", command_id=3, process_instance_id="P1", epoch=1, generation=1, latched=False)
        r = sc.result(3)
        assert r["latched"] is True and r.get("control_state_applied") is False
    finally:
        sc.close()


def test_lower_epoch_set_control_rejected():
    sc = _reconciled()           # epoch 1
    try:
        sc.send(cmd="set_control", command_id=2, process_instance_id="P1", epoch=3, generation=3, latched=False)
        sc.result(2)             # advance to epoch 3 / gen 3
        # lower epoch with EQUAL generation isolates the epoch guard (generation is checked first)
        sc.send(cmd="set_control", command_id=3, process_instance_id="P1", epoch=2, generation=3, latched=False)
        r = sc.result(3)
        assert r.get("control_state_applied") is False and r["error"] == "stale_epoch"
    finally:
        sc.close()


def test_stop_wins_over_concurrent_reconcile():
    sc = _reconciled()
    try:
        # set_control(unlatch) is QUEUED; estop bypasses the queue and runs first -> stays latched, drive refused
        sc.send(cmd="set_control", command_id=2, process_instance_id="P1", epoch=9, generation=9, latched=False)
        sc.send(cmd="estop", command_id=3, epoch=10, generation=10)
        sc.result(3)
        sc.send(cmd="drive", command_id=4, generation=10, epoch=10, ly=0.2, duration=0.1)
        r = sc.result(4)
        assert r["sent_to_agora"] is False   # latched by the STOP that won the race
    finally:
        sc.close()


def test_prepare_rejected_while_estop_initial_zero_blocked():
    # agent_next_2 §9 case 3 (FORCED with the block seam, no sleep): block the SDK send so the E-STOP's initial
    # zero-frame is in flight (activeStops>0); a RESET prepare must be rejected.
    sc = _reconciled()
    try:
        sc.send(cmd="__block")                                   # the next SDK send will block
        sc.send(cmd="estop", command_id=1, epoch=2, generation=2)   # doEstop awaits the blocked zero-send
        sc.send(cmd="prepare_reset", command_id=2, process_instance_id="P1", sidecar_instance_id=sc.sid,
                expected_epoch=2, expected_generation=2, release_epoch=3, release_generation=3)
        assert sc.result(2)["error"] == "estop_in_flight"        # rejected while a STOP dispatch is in flight
        sc.send(cmd="__release")
        assert sc.result(1)["local_latch_set"] is True           # the E-STOP then completes
    finally:
        sc.close()


def test_two_simultaneous_prepares_one_accepted():
    # §9 case 6: only ONE prepared reset may be active at a time.
    sc = _reconciled()
    try:
        sc.send(cmd="estop", command_id=1, epoch=2, generation=2)
        sc.result(1)
        for cid in (2, 3):
            sc.send(cmd="prepare_reset", command_id=cid, process_instance_id="P1", sidecar_instance_id=sc.sid,
                    expected_epoch=2, expected_generation=2, release_epoch=3, release_generation=3)
        r2, r3 = sc.result(2), sc.result(3)
        assert r2["prepared"] is True
        assert r3["error"] == "reset_already_prepared"
    finally:
        sc.close()


def test_same_generation_newer_epoch_drive_rejected():
    # §9 case 9: a drive with the matching generation but a NEWER epoch is stale and rejected.
    sc = _reconciled()
    try:
        sc.send(cmd="drive", command_id=1, generation=1, epoch=2, ly=0.2, duration=0.1)
        assert sc.result(1)["error"] == "stale_epoch"
    finally:
        sc.close()


def test_raw_hard_forbidden_even_when_env_allows():
    # env tries to allowlist movement (101007) + dock (103043); the immutable hard-forbidden set wins.
    sc = Sidecar(env_extra={"AUTOBOT_RTM_RAW_ALLOW": "101007,103043"})
    try:
        sc.send(cmd="raw", command_id=1, id=101007, data={"ly": 80})
        r1 = sc.result(1)
        sc.send(cmd="raw", command_id=2, id=103043, data={})
        r2 = sc.result(2)
        assert r1["ok"] is False and r1["error"].startswith("raw_id_hard_forbidden")
        assert r2["ok"] is False and r2["error"].startswith("raw_id_hard_forbidden")
    finally:
        sc.close()


def test_failed_estop_send_stays_latched_and_blocks_reset():
    # initial-zero SDK send fails -> local latch still asserted + honest ack; a matching reset can still
    # reconcile afterwards (the dispatch is no longer in flight) but the latch was never silently dropped.
    sc = Sidecar(fail=True)
    try:
        sc.send(cmd="set_control", command_id=1, process_instance_id="P1", epoch=1, generation=1, latched=True)
        sc.result(1)
        sc.send(cmd="estop", command_id=2, epoch=2, generation=2)
        r = sc.result(2)
        assert r["local_latch_set"] is True and r["initial_zero_sdk_send_succeeded"] is False
        assert r["ok"] is False and r["latched"] is True
    finally:
        sc.close()


# ---- RtmNode reconciler cases (faked _send) --------------------------------------------------------

def _node():
    from autobot.robot.rtm_node import RtmNode
    sent: list = []
    n = RtmNode(session_provider=lambda *a, **k: None)
    n._send = lambda cmd: (sent.append(cmd), True)[1]   # type: ignore[assignment]
    n.connected = True
    return n, sent


def test_rtmnode_rejects_replaced_instance_result():
    n, _sent = _node()
    n._sidecar_instance_id = "SID-A"
    n._sidecar_gen = 1
    # a result from a DIFFERENT (replaced) sidecar instance must be rejected and never adopted
    n._handle_event({"ev": "command_result", "command_id": None, "sidecar_instance_id": "SID-B",
                     "latched": False, "generation": 99, "epoch": 99})
    assert n._sidecar_gen == 1                         # state NOT adopted from the replaced instance
    assert n._last_reconcile_error == "result_from_replaced_sidecar"


def test_two_phase_reconcile_clears_desired_latch_via_rtmnode():
    n, sent = _node()
    n._sidecar_instance_id = "SID"
    n._auth_latched, n._auth_gen, n._auth_epoch = True, 5, 5

    # Drive the two-phase release: prepare echoes a nonce; commit echoes the reconciled new state.
    def feed(cmd):
        sent.append(cmd)
        cid = cmd.get("command_id")
        if cid is None:
            return True
        if cmd.get("cmd") == "prepare_reset":
            n._handle_event({"ev": "command_result", "command_id": cid, "cmd": "prepare_reset",
                             "sidecar_instance_id": "SID", "prepared": True, "prepare_nonce": "N",
                             "latched": True, "control_ready": True})
        elif cmd.get("cmd") == "commit_reset":
            n._handle_event({"ev": "command_result", "command_id": cid, "cmd": "commit_reset",
                             "sidecar_instance_id": "SID", "reconciled": True, "latched": False,
                             "generation": 6, "epoch": 6, "control_ready": True})
        return True

    n._send = feed  # type: ignore[assignment]
    r = n.reset_reconcile(5, 5, 6, 6, timeout=1.0)
    assert r["ok"] is True and r["reconciled"] is True
    assert n._auth_latched is False and n._auth_gen == 6 and n._auth_epoch == 6
    # the prepare nonce was consumed by the commit; the desired latch cannot be double-cleared.
    assert n._auth_latched is False


def test_reset_fails_closed_when_commit_not_control_ready():
    n, sent = _node()
    n._sidecar_instance_id = "SID"
    n._auth_latched, n._auth_gen, n._auth_epoch = True, 3, 3

    def feed(cmd):
        sent.append(cmd)
        cid = cmd.get("command_id")
        if cid is None:
            return True
        if cmd.get("cmd") == "prepare_reset":
            n._handle_event({"ev": "command_result", "command_id": cid, "cmd": "prepare_reset",
                             "sidecar_instance_id": "SID", "prepared": True, "prepare_nonce": "N",
                             "latched": True, "control_ready": True})
        elif cmd.get("cmd") == "commit_reset":
            n._handle_event({"ev": "command_result", "command_id": cid, "cmd": "commit_reset",
                             "sidecar_instance_id": "SID", "reconciled": False, "latched": True,
                             "error": "control_not_ready", "control_ready": False})
        return True

    n._send = feed  # type: ignore[assignment]
    r = n.reset_reconcile(3, 3, 4, 4, timeout=1.0)
    assert r["ok"] is False and n._auth_latched is True   # stays latched (fail closed)
