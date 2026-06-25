"""P0-R4 atomicity — ControlArbiter tokenized STOP tracking, reset admission/CAS, motion admission.

Forced-ordering tests (barriers/events where concurrency matters) prove: STOP always advances epoch+gen even
when inhibited; an older STOP finishing does not unblock a newer in-flight STOP; RESET is admission-gated +
single-use CAS; a motion ticket admitted before STOP is rejected after.
"""
from __future__ import annotations

import threading

from autobot.brain.safety import EFFECT_DOCK, EFFECT_LASER, EFFECT_MOTION, ControlArbiter


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


def test_finalize_reset_installs_a_strictly_newer_release_state():
    # agent_next_2 §2.1/§2.4: RESUME reserves + installs a brand-new post-resume epoch/generation, so any
    # command admitted before the completed RESUME stays stale forever.
    a = _stopped_ready_for_reset()          # epoch=1, gen=1, latched+inhibited
    pre_epoch, pre_gen = a.epoch(), a.generation()
    tok = a.begin_reset()
    assert tok.release_epoch > pre_epoch and tok.release_generation > pre_gen
    assert a.epoch() == pre_epoch and a.generation() == pre_gen   # reserved, NOT yet installed
    assert a.finalize_reset(tok) is True
    assert a.epoch() == tok.release_epoch and a.generation() == tok.release_generation
    assert not a.is_latched() and not a.is_master_inhibited()


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


def test_effect_tickets_are_classed_unique_and_invalidated_by_stop():
    # agent_next_2 §1.1: every effect carries an admitted ticket {epoch,gen,effect_class,ticket_id}.
    a = ControlArbiter()
    t_dock = a.admit_effect(EFFECT_DOCK)
    t_laser = a.admit_effect(EFFECT_LASER)
    t_motion = a.admit_motion()
    assert t_dock is not None and t_dock.effect_class == EFFECT_DOCK
    assert t_laser is not None and t_laser.effect_class == EFFECT_LASER
    assert t_motion is not None and t_motion.effect_class == EFFECT_MOTION
    ids = {t_dock.ticket_id, t_laser.ticket_id, t_motion.ticket_id}
    assert len(ids) == 3 and 0 not in ids        # unique, non-zero ticket ids
    assert all(a.validate_ticket(t) for t in (t_dock, t_laser, t_motion))
    nt = a.begin_master_stop()
    # every prior effect ticket is invalidated by the STOP (epoch advanced + latched)
    assert not any(a.validate_ticket(t) for t in (t_dock, t_laser, t_motion))
    assert a.admit_effect(EFFECT_DOCK) is None    # no admission while latched/inhibited
    a.end_estop_dispatch(nt)
    assert a.admit_effect(EFFECT_LASER) is None   # still latched after dispatch ends


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
