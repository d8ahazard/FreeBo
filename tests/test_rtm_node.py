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


def test_estop_reset_send_does_not_clear_desired_latch():
    # P0-R4 item 4: a bare estop_reset send must NOT mutate desired state early. The desired latch clears only
    # via reset_control() after the response is validated (see test_reset_success_clears_desired_latch).
    n, _ = _node()
    n.send_acked({"cmd": "estop", "generation": 7}, timeout=0.5)
    assert n._auth_latched is True
    n.send_acked({"cmd": "estop_reset", "generation": 7}, timeout=0.5)
    assert n._auth_latched is True          # still latched — fail closed


def test_command_result_echo_updates_sidecar_view():
    n, _ = _node()
    n._handle_event({"ev": "command_result", "command_id": None, "latched": True, "generation": 9})
    assert n._sidecar_latched is True
    assert n._sidecar_gen == 9


def test_control_state_reports_synchronization():
    n, _ = _node()
    n._auth_gen, n._auth_latched = 2, False
    n._sidecar_gen, n._sidecar_latched = 2, False
    assert n.control_state()["synchronized"] is True
    n._sidecar_gen = 3
    cs = n.control_state()
    assert cs["synchronized"] is False
    assert cs["process_generation"] == 2 and cs["sidecar_generation"] == 3


def test_fail_pending_releases_blocked_waiters():
    n, _ = _node()
    evt = threading.Event()
    n._pending[999] = {"event": evt, "result": None}
    n._fail_pending("sidecar exited")
    assert evt.is_set()
    assert 999 not in n._pending


def test_duplicate_command_result_is_ignored():
    n, _ = _node()
    # No pending slot for this id -> a late/duplicate result must not raise or mis-resolve anything.
    n._handle_event({"ev": "command_result", "command_id": 12345, "sent_to_agora": True,
                     "latched": False, "generation": 1})
    assert 12345 not in n._pending


def _node_reset(reset_resp: dict):
    """A node whose faked _send echoes `reset_resp` for estop_reset (and a normal ack for everything else)."""
    n = RtmNode(session_provider=lambda *_: {"ok": True})

    def fake_send(cmd: dict) -> bool:
        cid = cmd.get("command_id")
        if cid is not None:
            if cmd.get("cmd") == "estop_reset":
                n._handle_event({"ev": "command_result", "command_id": cid, "cmd": "estop_reset", **reset_resp})
            else:
                n._handle_event({"ev": "command_result", "command_id": cid, "cmd": cmd.get("cmd"),
                                 "sent_to_agora": True, "latched": cmd.get("cmd") == "estop",
                                 "generation": cmd.get("generation", 0)})
        return True

    n._send = fake_send  # type: ignore[assignment]
    return n


def test_reset_success_clears_desired_latch():
    n = _node_reset({"sent_to_agora": True, "latched": False, "generation": 5,
                     "rtm_connected": True, "control_ready": True})
    n._auth_latched, n._auth_gen = True, 5
    r = n.reset_control(5)
    assert r["ok"] is True and r["reconciled"] is True
    assert n._auth_latched is False and n._auth_gen == 5


def test_reset_missing_control_ready_fails_closed():
    n = _node_reset({"sent_to_agora": True, "latched": False, "generation": 5, "rtm_connected": True})
    n._auth_latched, n._auth_gen = True, 5
    r = n.reset_control(5)
    assert r["ok"] is False                     # control_ready absent -> not validated
    assert n._auth_latched is True              # desired state remains LATCHED (fail closed)


def test_reset_generation_mismatch_fails_closed():
    n = _node_reset({"sent_to_agora": True, "latched": False, "generation": 4,
                     "rtm_connected": True, "control_ready": True})
    n._auth_latched, n._auth_gen = True, 5
    r = n.reset_control(5)                       # asked for gen 5, sidecar echoed gen 4
    assert r["ok"] is False and n._auth_latched is True


def test_reset_disconnected_fails_closed():
    n = _node_reset({"sent_to_agora": True, "latched": False, "generation": 5,
                     "rtm_connected": False, "control_ready": True})
    n._auth_latched, n._auth_gen = True, 5
    assert n.reset_control(5)["ok"] is False and n._auth_latched is True


def test_failed_reset_keeps_process_latched_for_reconnect():
    n = _node_reset({"sent_to_agora": False, "latched": True, "generation": 5, "error": "stale_reset_generation"})
    n._auth_latched, n._auth_gen = True, 5
    n.reset_control(5)
    # A reconnect re-asserts the DESIRED state; it is still latched, so set_control would reassert latched=True.
    assert n.control_state()["process_latched"] is True
