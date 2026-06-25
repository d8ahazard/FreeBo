"""P0-R4 atomicity — ControlArbiter compare-and-swap + monotonic transition tests.

These prove the single process-side transition authority: STOP always advances epoch+generation (even when
already inhibited), and a RESET commits only via CAS — a newer STOP (advanced epoch) makes an older reset
fail, including under forced concurrent ordering.
"""
from __future__ import annotations

import threading

from autobot.brain.safety import ControlArbiter


def test_stop_advances_epoch_and_generation_even_when_already_inhibited():
    a = ControlArbiter()
    t1 = a.begin_master_stop()
    t2 = a.begin_master_stop()           # already inhibited, but a NEW transition identity
    assert t2["epoch"] > t1["epoch"]
    assert t2["generation"] > t1["generation"]
    assert a.master_inhibited and a.desired_latched


def test_reset_cas_succeeds_when_nothing_changed():
    a = ControlArbiter()
    a.begin_master_stop()
    a.end_estop_dispatch()
    tok = a.begin_reset()
    assert a.commit_reset(tok) is True
    assert a.desired_latched is False and a.master_inhibited is False


def test_reset_cas_fails_after_a_newer_stop():
    # The exact required ordering: STOP@epoch -> RESET captures epoch -> newer STOP advances -> RESET rejected.
    a = ControlArbiter()
    a.begin_master_stop()
    a.end_estop_dispatch()
    tok = a.begin_reset()                 # captures the current epoch/generation
    a.begin_master_stop()                 # a NEWER stop advances the epoch
    a.end_estop_dispatch()
    assert a.commit_reset(tok) is False   # the stale reset cannot clear anything
    assert a.desired_latched is True and a.master_inhibited is True


def test_estop_in_flight_blocks_reset_commit():
    a = ControlArbiter()
    a.begin_master_stop()                 # leaves estop_in_flight=True (no end_estop_dispatch)
    tok = a.begin_reset()
    assert a.commit_reset(tok) is False
    a.end_estop_dispatch()
    assert a.commit_reset(tok) is True    # now permitted (nothing else changed)


def test_concurrent_stop_during_reset_forces_cas_failure():
    # Force the race with barriers: the reset captures its token, then a STOP runs on another thread BEFORE
    # the reset commits. The commit must observe the advanced epoch and fail.
    a = ControlArbiter()
    a.begin_master_stop()
    a.end_estop_dispatch()
    tok = a.begin_reset()

    captured = threading.Barrier(2)
    stop_done = threading.Event()

    def stopper():
        captured.wait()            # both threads aligned at this point
        a.begin_master_stop()      # newer STOP advances epoch
        a.end_estop_dispatch()
        stop_done.set()

    t = threading.Thread(target=stopper)
    t.start()
    captured.wait()
    stop_done.wait(2.0)            # ensure the newer STOP landed before we attempt commit
    t.join(2.0)
    assert a.commit_reset(tok) is False
    assert a.desired_latched is True
