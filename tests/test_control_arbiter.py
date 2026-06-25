"""P0-R4 atomicity — ControlArbiter tokenized STOP tracking, reset admission/CAS, motion admission.

Forced-ordering tests (barriers/events where concurrency matters) prove: STOP always advances epoch+gen even
when inhibited; an older STOP finishing does not unblock a newer in-flight STOP; RESET is admission-gated +
single-use CAS; a motion ticket admitted before STOP is rejected after.
"""
from __future__ import annotations

import threading

from autobot.brain.safety import ControlArbiter


def _stopped_ready_for_reset() -> ControlArbiter:
    a = ControlArbiter()
    tok = a.begin_master_stop()
    a.end_estop_dispatch(tok)
    return a


def test_stop_advances_epoch_and_generation_even_when_already_inhibited():
    a = ControlArbiter()
    t1 = a.begin_master_stop()
    t2 = a.begin_master_stop()
    assert t2.epoch > t1.epoch and t2.generation > t1.generation
    assert t1.dispatch_id != t2.dispatch_id
    assert a.is_master_inhibited() and a.is_latched()


def test_overlapping_stops_older_finishing_first_keeps_in_flight():
    # Item 1 required ordering: A begins (blocked), B begins (blocked), A completes -> still in flight (B),
    # RESET must be refused; only after B completes is reset admissible.
    a = ControlArbiter()
    a_tok = a.begin_master_stop()
    b_tok = a.begin_master_stop()
    a.end_estop_dispatch(a_tok)             # older STOP completes first
    assert a.stop_in_flight() is True
    assert a.begin_reset() is None          # B still in flight -> reset refused
    a.end_estop_dispatch(b_tok)
    assert a.stop_in_flight() is False
    assert a.begin_reset() is not None      # now admissible


def test_reset_admission_rejected_when_not_latched():
    a = ControlArbiter()
    assert a.begin_reset() is None          # never stopped -> not latched/inhibited


def test_reset_admission_rejected_while_stop_in_flight():
    a = ControlArbiter()
    a.begin_master_stop()                   # not ended -> in flight
    assert a.begin_reset() is None


def test_only_one_active_reset_attempt():
    a = _stopped_ready_for_reset()
    t1 = a.begin_reset()
    assert t1 is not None
    assert a.begin_reset() is None          # a second concurrent reset is refused


def test_reset_cas_succeeds_when_unchanged():
    a = _stopped_ready_for_reset()
    tok = a.begin_reset()
    assert a.commit_reset(tok) is True
    assert not a.is_latched() and not a.is_master_inhibited()


def test_reset_token_is_single_use():
    a = _stopped_ready_for_reset()
    tok = a.begin_reset()
    assert a.commit_reset(tok) is True
    assert a.commit_reset(tok) is False     # cannot commit twice


def test_reset_cas_fails_after_a_newer_stop():
    a = _stopped_ready_for_reset()
    tok = a.begin_reset()
    nt = a.begin_master_stop()              # newer STOP advances epoch (also cancels the active reset id)
    a.end_estop_dispatch(nt)
    assert a.commit_reset(tok) is False
    assert a.is_latched() and a.is_master_inhibited()


def test_motion_admission_and_stale_ticket_after_stop():
    # Item 8: a ticket admitted before STOP must be rejected after STOP.
    a = ControlArbiter()
    ticket = a.admit_motion()
    assert ticket is not None and a.validate_ticket(ticket) is True
    nt = a.begin_master_stop()
    assert a.validate_ticket(ticket) is False    # stale (epoch advanced + latched)
    a.end_estop_dispatch(nt)
    assert a.admit_motion() is None              # still latched/inhibited


def test_concurrent_stop_blocks_reset_admission_under_barrier():
    a = _stopped_ready_for_reset()
    barrier = threading.Barrier(2)
    done = threading.Event()

    def stopper():
        barrier.wait()
        a.begin_master_stop()    # in flight (not ended) -> reset must be refused
        done.set()

    t = threading.Thread(target=stopper)
    t.start()
    barrier.wait()
    done.wait(2.0)
    t.join(2.0)
    assert a.begin_reset() is None
