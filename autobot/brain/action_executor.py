"""ActionExecutor — the single authoritative path from an authorized motion intent to physical movement.

This consolidates what used to be TWO confirmation paths (locomotion's immediate frame-diff and the agent's
next-cycle `MotionConfirmer`) into one lifecycle with sequence-aware evidence:

    PROPOSED -> AUTHORIZED -> EXECUTING -> AWAITING_EVIDENCE -> SUCCEEDED | FAILED | UNKNOWN | CANCELLED

It is deliberately **policy-free**: it executes, gathers evidence, enforces deadlines, and reaches a terminal
state. It does NOT decide what to do next — a navigator / candidate producer proposes a recovery as a *new*
authorized action with a `parent_id`. Key guarantees (see docs/SAFETY.md + the Phase 0 plan):

  * every motion goes through `SafetyFloor.check_drive` (the final mechanical clamp) — never bypassed;
  * after a pulse we WAIT for a frame whose sequence is newer than the 'before' frame. No fresh frame by the
    evidence deadline => UNKNOWN, **never** STUCK (a stale cached frame must not read as "didn't move");
  * **outcome mapping** (the evidence verdict drives the lifecycle): `moved` -> SUCCEEDED; `unknown` or
    unavailable evidence -> UNKNOWN; `stuck`/`blocked` -> FAILED (the physical move achieved no progress, so
    callers must NOT see ok=true). The evidence verdict itself is kept on `Action.result`;
  * `link.move()` is bounded by an EXECUTION deadline (a hung move coroutine -> stop + FAILED), and
    `link.stop()` is bounded too (shutdown can't hang); the evidence timeout is separate and does not protect
    against a hung move;
  * `stop()` always runs in `finally` (deadman);
  * a link rejection is FAILED immediately;
  * terminal state is set exactly once and a CANCELLED action is protected from late evidence/completion;
  * UNKNOWN never escalates to a more aggressive move — that is the caller's (gated) decision.
"""
from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

from ..diagnostics.motion import classify_motion, frame_diff

EmitFn = Optional[Callable[[dict], Awaitable[None]]]


class State(str, Enum):
    PROPOSED = "proposed"
    AUTHORIZED = "authorized"
    EXECUTING = "executing"
    AWAITING_EVIDENCE = "awaiting_evidence"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


TERMINAL = {State.SUCCEEDED, State.FAILED, State.UNKNOWN, State.CANCELLED}


class CancelToken:
    """A one-shot cancellation flag for an in-flight action (set by preemption / barge-in)."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


@dataclass
class Action:
    id: str
    kind: str                      # turn | step | drive | reverse | stop
    params: dict
    source: str                    # authorization source: ai | manual | reflex | recovery
    parent_id: Optional[str] = None
    state: State = State.PROPOSED
    reason: str = ""
    before_seq: Optional[int] = None
    after_seq: Optional[int] = None
    result: Optional[str] = None   # motion evidence: moved | stuck | blocked | unknown (None until evidenced)
    created_ts: float = field(default_factory=time.monotonic)
    ended_ts: Optional[float] = None
    cancel_token: CancelToken = field(default_factory=CancelToken)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "params": self.params, "source": self.source,
                "parent_id": self.parent_id, "state": self.state.value, "reason": self.reason,
                "before_seq": self.before_seq, "after_seq": self.after_seq, "result": self.result,
                "duration_ms": None if self.ended_ts is None else round((self.ended_ts - self.created_ts) * 1000, 1)}


def _coarse_kind(ly: float, rx: float) -> str:
    if abs(rx) > abs(ly):
        return "turn"
    if ly > 0:
        return "step"
    if ly < 0:
        return "reverse"
    return "stop"


async def back_up_sequence(executor: "ActionExecutor", *, settings, reverse, turn, source: str = "manual"):
    """A reverse pulse, then a turn ONLY IF the reverse actually moved. Any non-success reverse outcome
    (FAILED incl. stuck/blocked/link-reject/timeout, UNKNOWN, or CANCELLED) aborts the sequence — the
    sequence-level evidence requirement is NOT bypassed by source='manual'. The turn is a CHILD of the
    reverse. Returns (reverse_action, turn_action_or_None)."""
    ar = await executor.run_drive(reverse[0], reverse[1], reverse[2], settings=settings, source=source)
    if ar.state == State.SUCCEEDED and ar.result == "moved" and turn is not None:
        at = await executor.run_drive(turn[0], turn[1], turn[2], settings=settings, source=source,
                                      parent_id=ar.id)
        return ar, at
    return ar, None


class ActionExecutor:
    """One executor instance per brain. `run_drive` is the only thing that issues AI motion + evidence."""

    def __init__(self, link, safety, *, emit: EmitFn = None, metrics=None,
                 evidence_timeout: float = 1.5, settle: float = 0.3, poll: float = 0.05,
                 video_max_age: float = 2.0, hold_threshold: int = 2,
                 execution_grace: float = 3.0, stop_timeout: float = 2.0) -> None:
        self.link = link
        self.safety = safety
        self.emit = emit
        self.metrics = metrics
        self.evidence_timeout = evidence_timeout
        self.settle = settle
        self.poll = poll
        self.video_max_age = video_max_age      # refuse motion when the 'before' frame is older than this (s)
        self.hold_threshold = hold_threshold    # consecutive non-progress attempts before HOLD
        # Execution deadline = the pulse duration + this grace. A move coroutine that doesn't return by then is
        # treated as HUNG (stop + FAILED). `stop_timeout` bounds link.stop() so shutdown can't hang forever.
        self.execution_grace = execution_grace
        self.stop_timeout = stop_timeout
        self._active: Optional[Action] = None
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)
        # Circuit breaker: after `hold_threshold` consecutive failed/unknown/stuck/blocked attempts (incl.
        # oscillating recoveries), enter HOLD — refuse further motion until a manual reset / fresh authorization.
        self._hold = False
        self._nonprogress = 0
        # Stop-dispatch instrumentation (P0.8/Correction 3): the monotonic time IMMEDIATELY BEFORE link.stop()
        # is invoked (the acceptance reference) and when it returns (completion, a separate metric).
        self.last_stop_dispatch_ts = 0.0
        self.last_stop_complete_ts = 0.0

    # --- introspection / breaker ---
    def active(self) -> Optional[Action]:
        a = self._active
        return a if (a and a.state not in TERMINAL) else None

    def in_hold(self) -> bool:
        return self._hold

    def reset_breaker(self) -> None:
        """Leave HOLD and clear the non-progress counter. Called on a manual takeover / explicit resume — a
        deliberate, human-authorized decision (HOLD never clears itself by retrying)."""
        self._hold = False
        self._nonprogress = 0

    def _note_outcome(self, a: Action) -> None:
        """Fold a terminal action into the circuit breaker. Progress (SUCCEEDED == a confirmed 'moved') resets
        it; a cancelled action is intentional and ignored; the breaker-refusal action does not re-count itself.
        FAILED (incl. stuck/blocked/link-reject/timeout) and UNKNOWN are non-progress -> HOLD at the threshold."""
        if a.state == State.CANCELLED:
            return
        if a.state == State.FAILED and a.reason.startswith("circuit breaker"):
            return
        if a.state == State.SUCCEEDED:        # only a confirmed 'moved' reaches SUCCEEDED now
            self._nonprogress = 0
            self._hold = False
            return
        if a.state in (State.FAILED, State.UNKNOWN):
            self._nonprogress += 1
            if self._nonprogress >= self.hold_threshold:
                self._hold = True

    def _new_id(self) -> str:
        return f"act-{next(self._ids)}-{uuid.uuid4().hex[:6]}"

    async def _emit(self, ev: dict) -> None:
        if self.emit:
            try:
                await self.emit(ev)
            except Exception:  # noqa: BLE001
                pass

    async def _set_state(self, a: Action, state: State, reason: str = "") -> None:
        # Exactly-once terminal: never move out of (or re-enter) a terminal state.
        if a.state in TERMINAL:
            return
        a.state = state
        if reason:
            a.reason = reason
        if state in TERMINAL:
            a.ended_ts = time.monotonic()
        await self._emit({"type": "action", **a.to_dict(), "ts": time.time()})

    async def _sample(self):
        try:
            return await self.link.snapshot_sample()
        except Exception:  # noqa: BLE001
            return None

    async def _stop(self) -> None:
        # Bounded so a wedged link.stop() can't hang the deadman / shutdown. Record the dispatch timestamp
        # IMMEDIATELY before invoking link.stop() (the acceptance reference) and the completion timestamp after.
        self.last_stop_dispatch_ts = time.monotonic()
        try:
            await asyncio.wait_for(self.link.stop(), timeout=self.stop_timeout)
        except Exception:  # noqa: BLE001 (incl. asyncio.TimeoutError) - never hang on stop
            pass
        self.last_stop_complete_ts = time.monotonic()

    async def preempt(self, reason: str = "preempted") -> None:
        """Cancel the in-flight action (barge-in / manual takeover / reflex) and stop. The running `run_drive`
        observes the token at its next checkpoint and finishes as CANCELLED. Does not need the lock."""
        a = self._active
        if a is not None and a.state not in TERMINAL:
            a.cancel_token.cancel()
        await self._stop()

    async def _await_fresh(self, before_seq: Optional[int], token: CancelToken):
        """Poll for a frame whose sequence is strictly newer than `before_seq`, up to the evidence deadline.
        Returns the fresh FrameSample, or None (timeout / cancelled / unprovable freshness)."""
        deadline = time.monotonic() + self.evidence_timeout
        await asyncio.sleep(self.settle)   # let motion settle before the 'after' look
        while time.monotonic() < deadline:
            if token.cancelled:
                return None
            fs = await self._sample()
            if fs is not None and fs.valid:
                # seq is None on links that can't prove freshness -> we cannot confirm a NEW frame -> UNKNOWN.
                if before_seq is not None and fs.seq is not None and fs.seq > before_seq:
                    return fs
            await asyncio.sleep(self.poll)
        return None

    async def run_drive(self, ly: float, rx: float, duration: float, *, settings,
                        source: str = "ai", parent_id: Optional[str] = None) -> Action:
        """Execute ONE authorized motion pulse with sequence-aware evidence. Returns the terminal Action."""
        a = Action(id=self._new_id(), kind=_coarse_kind(ly, rx),
                   params={"ly": ly, "rx": rx, "duration": duration}, source=source, parent_id=parent_id)
        token = a.cancel_token
        before_jpeg = None
        async with self._lock:
            self._active = a
            t0 = time.perf_counter()
            try:
                await self._emit({"type": "action", **a.to_dict(), "ts": time.time()})
                # Circuit breaker: while in HOLD, refuse motion until a manual reset / fresh authorization.
                # (Recovery sources do NOT bypass HOLD — UNKNOWN/stuck must not escalate into more motion.)
                if self._hold and source in ("ai", "recovery"):
                    await self._set_state(a, State.FAILED, "circuit breaker: HOLD — manual reset required")
                    return a

                # before-frame (sequence anchor)
                before = await self._sample()
                if before is not None and before.valid:
                    a.before_seq = before.seq
                    before_jpeg = before.jpeg

                # Stale-video guard: never move on an old/missing frame (cloud stream can stall). This reads as
                # evidence UNKNOWN — it must NEVER provoke a more aggressive move. The limit is per-deployment
                # (settings.video_max_age_s), independent of the telemetry freshness budget.
                max_age = getattr(settings, "video_max_age_s", None) or self.video_max_age
                if source in ("ai", "recovery") and (before is None or not before.valid
                                                     or before.age > max_age):
                    a.result = "unknown"
                    await self._set_state(a, State.UNKNOWN,
                                          f"video stale/missing (age {getattr(before, 'age', None)}s "
                                          f"> {max_age}s) — not moving")
                    return a

                # authorize (final mechanical gate)
                d = self.safety.check_drive(settings, ly, rx, duration, source=source)
                if not d.allowed:
                    await self._set_state(a, State.FAILED, f"blocked: {d.reason}")
                    return a
                # P0 §3: ADMIT the motion — capture a MotionTicket bound to the CURRENT (epoch, generation).
                # admit_motion() returns None when inhibited/latched/STOP-in-flight, so a master STOP between
                # the clamp and here refuses motion. The ticket is carried to the link/sidecar and re-validated
                # right before dispatch, so a drive admitted before a STOP cannot reach the robot after it.
                ticket = self.safety.admit_motion()
                if ticket is None:
                    await self._set_state(a, State.FAILED, "blocked: motion not admitted (STOP/latched)")
                    return a
                a.params["ticket"] = {"epoch": ticket.epoch, "generation": ticket.generation}
                await self._set_state(a, State.AUTHORIZED)
                if token.cancelled:
                    await self._set_state(a, State.CANCELLED, "cancelled before execution")
                    return a

                # execute — bounded by an EXECUTION deadline so a HUNG move coroutine can't wedge the loop.
                await self._set_state(a, State.EXECUTING)
                # Re-validate the ticket IMMEDIATELY before the link call (a STOP may have landed during the
                # state transitions above). Stale ticket => FAILED, never motion.
                if not self.safety.validate_ticket(ticket):
                    await self._set_state(a, State.FAILED, "blocked: motion ticket superseded by STOP")
                    return a
                move_deadline = float(d.duration) + self.execution_grace
                try:
                    res = await asyncio.wait_for(
                        self.link.move(d.ly, d.rx, d.duration,
                                       generation=ticket.generation, epoch=ticket.epoch),
                        timeout=move_deadline)
                except asyncio.TimeoutError:
                    await self._stop()
                    await self._set_state(a, State.FAILED,
                                          f"execution timeout (move exceeded {move_deadline:.1f}s)")
                    return a
                if isinstance(res, dict) and res.get("ok") is False:
                    await self._set_state(a, State.FAILED, f"link rejected: {res.get('error') or 'move failed'}")
                    return a
                a.params["applied"] = {"ly": d.ly, "rx": d.rx, "duration": d.duration}

                # evidence: require a NEW frame, else UNKNOWN (never STUCK on a stale frame)
                await self._set_state(a, State.AWAITING_EVIDENCE)
                after = await self._await_fresh(a.before_seq, token)
                if token.cancelled:
                    await self._set_state(a, State.CANCELLED, "cancelled")
                    return a
                if after is None:
                    a.result = "unknown"
                    await self._set_state(a, State.UNKNOWN, "no fresh frame within evidence deadline")
                    return a
                a.after_seq = after.seq
                fd = frame_diff(before_jpeg, after.jpeg) if (before_jpeg and after.jpeg) else None
                mr = classify_motion(fd, expected="translate" if abs(ly) >= abs(rx) else "rotate")
                a.result = mr.state                      # evidence verdict: moved | stuck | blocked | unknown
                # Outcome mapping (P0.4): only a confirmed 'moved' is SUCCEEDED; stuck/blocked are physical
                # FAILUREs (no progress) so callers don't read ok=true; inconclusive evidence is UNKNOWN.
                if mr.state == "moved":
                    await self._set_state(a, State.SUCCEEDED, mr.detail)
                elif mr.state == "unknown":
                    await self._set_state(a, State.UNKNOWN, mr.detail or "evidence inconclusive")
                else:
                    await self._set_state(a, State.FAILED, mr.detail)
                return a
            except Exception as e:  # noqa: BLE001 - any failure stops the robot and is FAILED, never motion
                await self._set_state(a, State.FAILED, f"{type(e).__name__}: {e}")
                return a
            finally:
                await self._stop()                       # deadman: always stop after a pulse
                self._note_outcome(a)                    # fold the terminal result into the circuit breaker
                if self.metrics is not None:
                    try:
                        self.metrics.record("execute", (time.perf_counter() - t0) * 1000.0)
                    except Exception:  # noqa: BLE001
                        pass
