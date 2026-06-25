"""Phase 0.7 — the single authoritative ActionExecutor: lifecycle, sequence-aware evidence, cancellation."""
from __future__ import annotations

import asyncio

import pytest
from conftest import settings

from autobot.brain.action_executor import TERMINAL, Action, ActionExecutor, State, back_up_sequence
from autobot.brain.safety import SafetyFloor
from autobot.robot.mock_link import MockRobotLink


@pytest.fixture(autouse=True)
def _no_ffmpeg(monkeypatch):
    # Force the mock's instant solid-JPEG fallback (no ffmpeg testsrc2) so evidence polling is fast + stable.
    monkeypatch.setattr("autobot.robot.mock_link.shutil.which", lambda _x: None)


def _ex(link, **kw):
    sf = SafetyFloor()
    sf.begin_tick()
    params = {"evidence_timeout": 0.4, "settle": 0.02, "poll": 0.01}
    params.update(kw)
    return ActionExecutor(link, sf, **params)


def _auto():
    return settings(autonomy="auto", allow_motion=True, max_speed=0.6, max_move_duration=2.5)


class _MovingLink(MockRobotLink):
    """A mock whose camera visibly CHANGES each frame, so a real move reads 'moved' -> SUCCEEDED."""
    def __init__(self):
        super().__init__()
        self._lvl = 0

    async def snapshot_sample(self):
        import time as _t

        import cv2
        import numpy as np
        from autobot.robot.media_hub import FrameSample
        if not self._freeze_seq:
            self._seq += 1
            self._lvl += 80
        ok, buf = cv2.imencode(".jpg", np.full((48, 64, 3), self._lvl % 256, dtype=np.uint8))
        return FrameSample(jpeg=buf.tobytes() if ok else b"x", seq=self._seq,
                           wall_ts=_t.monotonic(), age=0.0, valid=True)


async def test_moved_is_succeeded():
    pytest.importorskip("cv2"); pytest.importorskip("numpy")
    a = await _ex(_MovingLink()).run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    assert a.state == State.SUCCEEDED and a.result == "moved"
    assert a.after_seq is not None and a.before_seq is not None and a.after_seq > a.before_seq


async def test_stuck_is_failed_not_succeeded():
    # MockRobotLink (solid JPEG) -> identical frames -> 'stuck' -> FAILED (P0.4), NOT SUCCEEDED.
    a = await _ex(MockRobotLink()).run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    assert a.state == State.FAILED and a.result == "stuck"


async def test_execution_timeout_is_failed():
    class _HangLink(MockRobotLink):
        async def move(self, ly, rx, duration, *, generation=None, epoch=None, ticket_id=None):
            await asyncio.sleep(10.0)            # hung move coroutine
            return {"ok": True}
    ex = _ex(_HangLink(), execution_grace=0.1)   # deadline = duration + 0.1s
    a = await ex.run_drive(0.5, 0.0, 0.2, settings=_auto(), source="ai")
    assert a.state == State.FAILED and "execution timeout" in a.reason


async def test_stale_stream_is_unknown_never_stuck():
    link = MockRobotLink()
    a0 = await link.snapshot_sample()    # advance seq once
    assert a0.seq is not None
    link._freeze_seq = True              # stalled stream: seq never advances
    ex = _ex(link)
    a = await ex.run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    assert a.state == State.UNKNOWN and a.result == "unknown"


async def test_safety_block_is_failed():
    ex = _ex(MockRobotLink())
    a = await ex.run_drive(0.5, 0.0, 0.3, settings=settings(autonomy="manual"), source="ai")
    assert a.state == State.FAILED and "blocked" in a.reason


async def test_link_rejection_is_failed():
    class _Reject(MockRobotLink):
        async def move(self, ly, rx, duration, *, generation=None, epoch=None, ticket_id=None):
            return {"ok": False, "error": "drive_rejected"}
    a = await _ex(_Reject()).run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    assert a.state == State.FAILED and "link rejected" in a.reason


async def test_preempt_yields_cancelled_not_failed():
    ex = _ex(MockRobotLink(), evidence_timeout=1.0, settle=0.3)
    task = asyncio.create_task(ex.run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai"))
    await asyncio.sleep(0.05)
    await ex.preempt()
    a = await task
    assert a.state == State.CANCELLED


async def test_recovery_is_a_child_action():
    ex = _ex(MockRobotLink())
    parent = await ex.run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    child = await ex.run_drive(0.0, 0.4, 0.3, settings=_auto(), source="recovery", parent_id=parent.id)
    assert child.parent_id == parent.id and child.source == "recovery"


async def test_terminal_state_is_exactly_once():
    ex = _ex(MockRobotLink())
    a = await ex.run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    assert a.state in TERMINAL
    first = a.state
    await ex._set_state(a, State.EXECUTING, "should be ignored")   # cannot leave a terminal state
    assert a.state is first


async def test_stop_dispatch_timestamps_recorded():
    ex = _ex(MockRobotLink())
    await ex.run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    # dispatch is recorded immediately before link.stop(); completion strictly after.
    assert ex.last_stop_dispatch_ts > 0.0
    assert ex.last_stop_complete_ts >= ex.last_stop_dispatch_ts


async def test_active_is_none_after_completion():
    ex = _ex(MockRobotLink())
    await ex.run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    assert ex.active() is None


# --- Phase 0.8: circuit breaker + freshness guards ---

async def test_two_nonprogress_attempts_enter_hold():
    link = MockRobotLink()
    await link.snapshot_sample()
    link._freeze_seq = True                       # stalled -> every attempt is UNKNOWN
    ex = _ex(link)
    a1 = await ex.run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    a2 = await ex.run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    assert a1.state == State.UNKNOWN and a2.state == State.UNKNOWN
    assert ex.in_hold() is True
    a3 = await ex.run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    assert a3.state == State.FAILED and "circuit breaker" in a3.reason


async def test_reset_breaker_resumes():
    link = MockRobotLink()
    await link.snapshot_sample(); link._freeze_seq = True
    ex = _ex(link)
    await ex.run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    await ex.run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    assert ex.in_hold()
    ex.reset_breaker()
    assert ex.in_hold() is False


def test_moved_result_resets_breaker():
    ex = _ex(MockRobotLink())
    ex._nonprogress = 1
    ex._note_outcome(Action(id="x", kind="step", params={}, source="ai",
                            state=State.SUCCEEDED, result="moved"))
    assert ex._nonprogress == 0 and ex.in_hold() is False


# --- Correction 2: BACK_UP only turns after a confirmed 'moved' reverse ---

class _ScriptedExec:
    """A fake executor that returns scripted Action outcomes and records every run_drive call."""
    def __init__(self, outcomes):
        self.calls = []
        self._outcomes = outcomes
        self._i = 0

    async def run_drive(self, ly, rx, duration, *, settings, source="ai", parent_id=None):
        self.calls.append({"ly": ly, "rx": rx, "source": source, "parent_id": parent_id})
        a = self._outcomes[self._i]
        self._i += 1
        return a


def _act(state, result=None):
    return Action(id="rev", kind="reverse", params={}, source="manual", state=state, result=result)


async def test_back_up_turns_only_after_moved():
    ex = _ScriptedExec([_act(State.SUCCEEDED, "moved"),               # reverse moved
                        _act(State.SUCCEEDED, "moved")])              # the child turn
    rev, turn = await back_up_sequence(ex, settings=_auto(), reverse=(-0.4, 0, 0.4), turn=(0, 0.4, 0.4))
    assert len(ex.calls) == 2 and ex.calls[1]["parent_id"] == "rev"   # reverse + child turn
    assert turn is not None


async def test_back_up_aborts_on_each_non_success():
    for state, result in [(State.FAILED, "stuck"), (State.FAILED, "blocked"), (State.FAILED, "link rejected"),
                          (State.FAILED, "execution timeout"), (State.UNKNOWN, "unknown"),
                          (State.CANCELLED, None)]:
        ex = _ScriptedExec([_act(state, result)])
        rev, turn = await back_up_sequence(ex, settings=_auto(), reverse=(-0.4, 0, 0.4), turn=(0, 0.4, 0.4))
        assert len(ex.calls) == 1, f"a turn was issued after reverse {state}/{result}"
        assert turn is None


async def test_stale_video_refuses_motion():
    import time as _t

    from autobot.robot.media_hub import FrameSample

    class _Stale(MockRobotLink):
        def __init__(self):
            super().__init__()
            self.moves = 0

        async def move(self, ly, rx, duration, *, generation=None, epoch=None, ticket_id=None):
            self.moves += 1
            return await super().move(ly, rx, duration, generation=generation, epoch=epoch, ticket_id=ticket_id)

        async def snapshot_sample(self):
            return FrameSample(jpeg=b"x", seq=1, wall_ts=_t.monotonic(), age=99.0, valid=True)

    link = _Stale()
    a = await _ex(link).run_drive(0.5, 0.0, 0.3, settings=_auto(), source="ai")
    assert a.state == State.UNKNOWN and "stale" in a.reason
    assert link.moves == 0          # never issued a move on a stale frame
