"""The cerebellum: closed-loop motor control.

The EBO drivetrain is non-linear with large deadbands and a touchy, fast turn (see docs/MOTION.md). Sending
raw `ly/rx/duration` open-loop is exactly why the robot over-rotates into walls. This module converts simple
INTENTS into reliable motion by pulsing the motors and watching the CAMERA (visual_motion) to confirm/steer:

  * `turn(degrees)` — pivot in small, re-measured increments toward a (capped) relative heading change.
  * `step()`        — one short, deadband-safe forward step, confirmed by frame-diff.
  * `drive(ly,rx,duration)` — a compatibility shim for existing call sites: it ignores the (unreliable) raw
    magnitudes and routes by intent (dominant axis -> turn or step) using the hard-coded motion model.

Everything still goes through `safety.check_drive` (the unchanged mechanical floor). Pure-ish: takes a
RobotLink + SafetyFloor + Settings snapshot + optional MotionProfile; no global state. CPU only.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from . import motion_model, supervisor, visual_motion
from .motion_profile import MotionProfile

SETTLE = 0.5             # let motion settle before the "after" snapshot
MAX_TURN_PULSES = 5      # cap closed-loop turn iterations (re-checkable increments, never a runaway)
TURN_TOLERANCE_DEG = 12  # "close enough" — we can't be precise without odometry, and don't need to be
YAW_CONF_MIN = 0.15      # phase-correlation confidence below this => fall back to the deg/s estimate
DEADBAND_BUMP = 0.04     # if a pulse didn't move the view, raise the command this much to beat static friction

EmitFn = Optional[Callable[[dict], Awaitable[None]]]


def _profile(settings, profile: Optional[MotionProfile]) -> tuple[MotionProfile, Any]:
    m = motion_model.for_variant(getattr(settings, "robot_variant", "AIR2"))
    return (profile or MotionProfile.from_model(m)), m


async def _snap(link) -> Optional[bytes]:
    try:
        jpeg, _ = await link.snapshot()
        return jpeg
    except Exception:  # noqa: BLE001
        return None


async def _emit(emit: EmitFn, ev: dict) -> None:
    if emit:
        try:
            await emit(ev)
        except Exception:  # noqa: BLE001
            pass


async def turn(*, link, safety, settings, profile: Optional[MotionProfile] = None,
               degrees: float, source: str = "ai", emit: EmitFn = None) -> dict:
    """Pivot in place toward a relative heading change, in small camera-measured increments. `degrees` is
    capped to MAX_TURN_DEG so a single intent can never spin the robot a long way blind."""
    prof, _m = _profile(settings, profile)
    import time as _t
    target = max(-motion_model.MAX_TURN_DEG, min(motion_model.MAX_TURN_DEG, float(degrees)))
    sign = 1.0 if target >= 0 else -1.0
    rx_mag = max(prof.turn_deadband + 0.02, min(prof.turn_rx, prof.turn_max))
    accumulated = 0.0
    stuck = 0
    for _i in range(MAX_TURN_PULSES):
        if abs(target - accumulated) <= TURN_TOLERANCE_DEG:
            break
        before = await _snap(link)
        d = safety.check_drive(settings, 0.0, sign * rx_mag, prof.turn_duration, source=source)
        if not d.allowed:
            return {"ok": False, "blocked": d.reason, "measured_deg": round(accumulated, 1)}
        if abs(d.rx) < 1e-6:   # scope forced no rotation (shouldn't happen for turns, but be safe)
            return {"ok": False, "blocked": "turn not permitted by scope", "measured_deg": round(accumulated, 1)}
        tk = safety.admit_motion()   # P0 §3: ticket each pulse so a STOP mid-turn refuses the next move
        if tk is None:
            return {"ok": False, "blocked": "motion not admitted (STOP/latched)",
                    "measured_deg": round(accumulated, 1)}
        await link.move(d.ly, d.rx, d.duration, generation=tk.generation, epoch=tk.epoch, ticket_id=tk.ticket_id)
        await asyncio.sleep(d.duration + SETTLE)
        after = await _snap(link)
        meas = visual_motion.measure(before, after)
        diff = meas.get("diff")
        moved = diff is not None and diff >= prof.move_threshold
        if not moved:
            stuck += 1
            rx_mag = min(prof.turn_max, rx_mag + DEADBAND_BUMP)   # overcome the deadband / static friction
            if stuck >= 2:
                return {"ok": True, "state": "stuck", "measured_deg": round(accumulated, 1),
                        "detail": "pivot produced no view change (deadband/obstruction)"}
            continue
        stuck = 0
        conf = float(meas.get("confidence", 0.0))
        if "est_yaw_deg" in meas and conf >= YAW_CONF_MIN:
            step_deg = abs(meas["est_yaw_deg"])            # camera-measured (reliable for small turns)
        else:
            step_deg = prof.turn_deg_per_s * d.duration    # big/blurred turn: fall back to the rate estimate
        accumulated += sign * step_deg
    await link.stop()
    res = {"ok": True, "state": "moved", "target_deg": round(target, 1),
           "measured_deg": round(accumulated, 1)}
    await _emit(emit, {"type": "motion", "kind": "turn", **res, "ts": _t.time()})
    return res


async def step(*, link, safety, settings, profile: Optional[MotionProfile] = None,
               strength: float = 1.0, source: str = "ai", emit: EmitFn = None) -> dict:
    """One short forward step, sized above the forward deadband and confirmed by frame-diff. We have no
    distance sense, so this is deliberately ONE small step — the brain re-looks and steps again if it wants."""
    prof, m = _profile(settings, profile)
    import time as _t
    ly = max(prof.forward_deadband + 0.05, prof.forward_speed)
    ly = min(ly, m.forward_max)
    dur = max(0.2, min(prof.forward_duration * max(0.5, min(2.0, strength)), 1.0))
    before = await _snap(link)
    # Optional smart supervisor: vet the path before committing to a forward step (off by default; the
    # cerebellum's own confirmation + the reflex are the primary protection).
    if source == "ai" and supervisor.enabled(settings) and before is not None:
        allow, reason = await supervisor.vet_step(before, settings)
        if not allow:
            import time as _t
            await _emit(emit, {"type": "thought", "text": f"({reason} — not stepping)", "ts": _t.time()})
            return {"ok": True, "state": "blocked", "detail": reason}
    d = safety.check_drive(settings, ly, 0.0, dur, source=source)
    if not d.allowed:
        return {"ok": False, "blocked": d.reason}
    if abs(d.ly) < 1e-6:   # scope is rotate-only (conversational/adjust) -> can't step forward
        return {"ok": False, "blocked": "forward not permitted right now (rotate-only scope)"}
    tk = safety.admit_motion()   # P0 §3: ticket the step
    if tk is None:
        return {"ok": False, "blocked": "motion not admitted (STOP/latched)"}
    await link.move(d.ly, d.rx, d.duration, generation=tk.generation, epoch=tk.epoch, ticket_id=tk.ticket_id)
    await asyncio.sleep(d.duration + SETTLE)
    after = await _snap(link)
    await link.stop()
    meas = visual_motion.measure(before, after)
    diff = meas.get("diff")
    # A cerebellum step is deliberately SHORT, so any clear view change above the still/noise floor counts as
    # real progress ("moved"). Only a near-identical view is "stuck"; "blocked" is reserved for the narrow
    # band just above noise (barely budged, likely obstructed). This keeps short steps from reading as blocked
    # and making the brain spin instead of driving.
    move_gate = max(0.02, m.still_diff * 2.0)   # short step: clearly-above-noise view change = real progress
    if diff is None:
        state = "unknown"
    elif diff >= move_gate:
        state = "moved"
    elif diff <= m.still_diff:
        state = "stuck"
    else:
        state = "blocked"
    res = {"ok": True, "state": state, "diff": diff, "drove": {"ly": d.ly, "duration": d.duration}}
    await _emit(emit, {"type": "motion", "kind": "step", **res, "ts": _t.time()})
    return res


async def drive(*, link, safety, settings, profile: Optional[MotionProfile] = None,
                ly: float, rx: float, duration: float = 0.0, source: str = "ai", emit: EmitFn = None) -> dict:
    """Compatibility shim for call sites that still compute (ly, rx, duration). The raw magnitudes are NOT
    trusted (they predate the measured deadbands) — we route by INTENT: dominant turn -> a capped pivot;
    dominant forward -> one step; backward -> a short reverse; otherwise stop."""
    ly = float(ly or 0.0)
    rx = float(rx or 0.0)
    if abs(rx) >= abs(ly) and abs(rx) > 1e-6:
        # One controlled turn increment toward the requested side; the brain re-decides next tick.
        deg = (1.0 if rx > 0 else -1.0) * motion_model.STEP_TURN_DEG
        return await turn(link=link, safety=safety, settings=settings, profile=profile,
                          degrees=deg, source=source, emit=emit)
    if ly > 1e-6:
        return await step(link=link, safety=safety, settings=settings, profile=profile,
                          source=source, emit=emit)
    if ly < -1e-6:
        # Reverse: a short, deadband-safe backward nudge (no camera confirmation forward of us anyway).
        prof, m = _profile(settings, profile)
        back = -max(prof.forward_deadband + 0.05, prof.forward_speed)
        d = safety.check_drive(settings, back, 0.0, min(prof.forward_duration, 0.6), source=source)
        if not d.allowed:
            return {"ok": False, "blocked": d.reason}
        tk = safety.admit_motion()   # P0 §3: ticket the reverse nudge
        if tk is None:
            return {"ok": False, "blocked": "motion not admitted (STOP/latched)"}
        await link.move(d.ly, d.rx, d.duration, generation=tk.generation, epoch=tk.epoch, ticket_id=tk.ticket_id)
        await link.stop()
        return {"ok": True, "state": "moved", "drove": {"ly": d.ly, "duration": d.duration}}
    res = await link.stop()
    return {"ok": res.get("ok", True), "state": "stopped"}
