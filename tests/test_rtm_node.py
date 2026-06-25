"""P0-R4.4 unit tests for RtmNode generation/latch reconciliation (no real sidecar process).

We stub `_send` so send_acked resolves synchronously via a fake correlated command_result, letting us assert
the pure reconciliation logic: drives are stamped with the authoritative generation, estop/estop_reset adopt
it, command_result echoes update the sidecar view, control_state reports synchronization, and pending
waiters are released on a sidecar exit.
"""
from __future__ import annotations

import threading

from autobot.robot.rtm_node import RtmNode


def _node():
    n = RtmNode(session_provider=lambda *_: {"ok": True})
    captured: list[dict] = []

    def fake_send(cmd: dict) -> bool:
        captured.append(cmd)
        cid = cmd.get("command_id")
        if cid is not None:
            # Echo a correlated result the way the sidecar would, including its latch+generation.
            n._handle_event({
                "ev": "command_result", "command_id": cid, "cmd": cmd.get("cmd"),
                "sent_to_agora": True, "error": None,
                "latched": cmd.get("cmd") == "estop", "generation": cmd.get("generation", 0),
            })
        return True

    n._send = fake_send  # type: ignore[assignment]
    return n, captured


def test_drive_is_stamped_with_authoritative_generation():
    n, cap = _node()
    n._auth_gen = 3
    r = n.send_acked({"cmd": "drive", "ly": 0.1, "rx": 0.0})
    assert r["ok"] is True
    assert cap[0]["generation"] == 3        # the drive carries the current generation for stale-rejection


def test_estop_adopts_generation_and_latches():
    n, _ = _node()
    n.send_acked({"cmd": "estop", "generation": 7}, timeout=0.5)
    assert n._auth_latched is True
    assert n._auth_gen == 7


def test_prepare_reset_send_does_not_clear_desired_latch():
    # agent_next_2 §2: phase 1 (prepare) NEVER clears the desired latch. Only a validated commit does.
    n, _ = _node()
    n.send_acked({"cmd": "estop", "generation": 7}, timeout=0.5)
    assert n._auth_latched is True
    n.send_acked({"cmd": "prepare_reset", "expected_epoch": 0, "expected_generation": 7,
                  "release_epoch": 1, "release_generation": 8}, timeout=0.5)
    assert n._auth_latched is True          # still latched — only commit_reset unlatches


def test_command_result_echo_updates_sidecar_view():
    n, _ = _node()
    n._handle_event({"ev": "command_result", "command_id": None, "latched": True, "generation": 9})
    assert n._sidecar_latched is True
    assert n._sidecar_gen == 9


def test_control_state_reports_synchronization():
    n, _ = _node()
    # P0 §2/§5: synchronized requires a bound sidecar instance, control_ready, and matching
    # epoch/generation/latch — not just generation+latch.
    n._auth_gen, n._auth_epoch, n._auth_latched = 2, 5, False
    n._sidecar_gen, n._sidecar_epoch, n._sidecar_latched = 2, 5, False
    n._sidecar_instance_id = "SID"
    n._sidecar_control_ready = True
    n._sidecar_accepted_process = n._process_instance_id
    assert n.control_state()["synchronized"] is True
    # any single divergence breaks synchronization
    n._sidecar_gen = 3
    cs = n.control_state()
    assert cs["synchronized"] is False
    assert cs["process_generation"] == 2 and cs["sidecar_generation"] == 3
    n._sidecar_gen = 2
    assert n.control_state()["synchronized"] is True
    n._sidecar_epoch = 6        # epoch mismatch also desyncs
    assert n.control_state()["synchronized"] is False
    n._sidecar_epoch = 5
    n._sidecar_control_ready = False   # control not ready desyncs
    assert n.control_state()["synchronized"] is False


def test_fail_pending_releases_blocked_waiters():
    n, _ = _node()
    evt = threading.Event()
    n._pending[999] = {"event": evt, "result": None}
    n._fail_pending("sidecar exited")
    assert evt.is_set()
    assert 999 not in n._pending


def test_result_wrong_kind_cannot_satisfy_waiter():
    # agent_next_2 §3.3: a result with the correct command id but the WRONG command kind must not resolve a
    # waiter (and must not adopt state from the mismatched result).
    n, _ = _node()
    n._sidecar_instance_id = "SID"
    evt = threading.Event()
    n._pending[7] = {"event": evt, "result": None, "kind": "commit_reset"}
    n._handle_event({"ev": "command_result", "command_id": 7, "cmd": "drive", "sidecar_instance_id": "SID",
                     "sent_to_agora": True, "generation": 99})
    assert not evt.is_set() and n._pending[7]["result"] is None
    assert n._sidecar_gen != 99                         # mismatched result did not mutate observed state
    # the correct kind resolves it
    n._handle_event({"ev": "command_result", "command_id": 7, "cmd": "commit_reset", "sidecar_instance_id": "SID",
                     "reconciled": True, "latched": False})
    assert evt.is_set()


def test_duplicate_command_result_is_ignored():
    n, _ = _node()
    # No pending slot for this id -> a late/duplicate result must not raise or mis-resolve anything.
    n._handle_event({"ev": "command_result", "command_id": 12345, "sent_to_agora": True,
                     "latched": False, "generation": 1})
    assert 12345 not in n._pending


def _node_reset(commit_resp: dict, prepare_ok: bool = True):
    """A node whose faked _send drives the two-phase release: prepare_reset echoes a prepared+nonce result (or a
    failure when prepare_ok=False), commit_reset echoes `commit_resp`, everything else a normal ack. The bound
    sidecar instance is 'SID' so results are accepted."""
    n = RtmNode(session_provider=lambda *_: {"ok": True})
    n._sidecar_instance_id = "SID"

    def fake_send(cmd: dict) -> bool:
        cid = cmd.get("command_id")
        if cid is None:
            return True
        kind = cmd.get("cmd")
        if kind == "prepare_reset":
            ev = ({"prepared": True, "prepare_nonce": "N", "latched": True, "control_ready": True}
                  if prepare_ok else {"prepared": False, "error": "stale_state"})
            n._handle_event({"ev": "command_result", "command_id": cid, "cmd": "prepare_reset",
                             "sidecar_instance_id": "SID", **ev})
        elif kind == "commit_reset":
            n._handle_event({"ev": "command_result", "command_id": cid, "cmd": "commit_reset",
                             "sidecar_instance_id": "SID", **commit_resp})
        else:
            n._handle_event({"ev": "command_result", "command_id": cid, "cmd": kind, "sidecar_instance_id": "SID",
                             "sent_to_agora": True, "latched": kind == "estop", "generation": cmd.get("generation", 0)})
        return True

    n._send = fake_send  # type: ignore[assignment]
    return n


def test_reset_success_clears_desired_latch():
    n = _node_reset({"reconciled": True, "latched": False, "generation": 6, "epoch": 6, "control_ready": True})
    n._auth_latched, n._auth_gen, n._auth_epoch = True, 5, 5
    r = n.reset_reconcile(5, 5, 6, 6)
    assert r["ok"] is True and r["reconciled"] is True
    assert n._auth_latched is False and n._auth_gen == 6 and n._auth_epoch == 6


def test_reset_missing_control_ready_fails_closed():
    n = _node_reset({"reconciled": True, "latched": False, "generation": 6, "epoch": 6})   # no control_ready
    n._auth_latched, n._auth_gen, n._auth_epoch = True, 5, 5
    r = n.reset_reconcile(5, 5, 6, 6)
    assert r["ok"] is False and n._auth_latched is True     # fail closed


def test_reset_generation_mismatch_fails_closed():
    n = _node_reset({"reconciled": True, "latched": False, "generation": 4, "epoch": 6, "control_ready": True})
    n._auth_latched, n._auth_gen, n._auth_epoch = True, 5, 5
    r = n.reset_reconcile(5, 5, 6, 6)                       # asked release gen 6, sidecar echoed gen 4
    assert r["ok"] is False and n._auth_latched is True


def test_reset_epoch_mismatch_fails_closed():
    n = _node_reset({"reconciled": True, "latched": False, "generation": 6, "epoch": 99, "control_ready": True})
    n._auth_latched, n._auth_gen, n._auth_epoch = True, 5, 5
    assert n.reset_reconcile(5, 5, 6, 6)["ok"] is False and n._auth_latched is True


def test_failed_prepare_keeps_process_latched():
    n = _node_reset({"reconciled": True, "latched": False, "generation": 6, "epoch": 6, "control_ready": True},
                    prepare_ok=False)
    n._auth_latched, n._auth_gen, n._auth_epoch = True, 5, 5
    r = n.reset_reconcile(5, 5, 6, 6)
    assert r["ok"] is False and r["phase"] == "prepare"
    assert n.control_state()["process_latched"] is True     # never reached commit; stays latched
