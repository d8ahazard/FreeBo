"""Movement calibration + profile for vision-only robots (EBO Air 2: no ToF/IMU/odometry).

We can't measure metric distance, but we CAN measure how much the camera view changes for a given drive
command — and from that derive a *controlled* step size so the robot takes small, observed moves instead of
long blind lunges. `calibrate()` runs a short grid of test moves in open space, measures the before/after
`frame_diff` (and advisory VSLAM yaw), and writes a `MotionProfile` to data/motion_profile.json:

  * baseline      — camera diff while STILL (compression/scene noise floor)
  * forward       — the smallest (speed, duration) burst that reliably "moved" the view -> the safe step
  * turn          — the smallest (rx, duration) burst that reliably changed the view -> the turn step
  * move_threshold— baseline-scaled gate the live motion-check / agent can trust on THIS robot/scene

Everything routes through the link's clamped move/stop; the caller (API) e-stops + restores after. Fail-soft.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..robot.link import RobotLink
from ..diagnostics.motion import classify_motion, frame_diff

PROFILE_PATH = Path(os.environ.get("AUTOBOT_MOTION_PROFILE", "data/motion_profile.json"))

# Test grid (kept small — each entry physically drives the robot). Speeds/durations are conservative.
_FWD_GRID = [(0.6, 0.4), (0.6, 0.7), (0.7, 1.0)]   # (speed, duration_s)
_TURN_GRID = [(0.6, 0.4), (0.6, 0.7)]              # (rx, duration_s)
_SETTLE = 0.6                                       # let motion settle before the "after" snapshot


@dataclass
class MotionProfile:
    forward_speed: float = 0.6
    forward_duration: float = 0.7
    turn_rx: float = 0.6
    turn_duration: float = 0.6
    baseline: float = 0.006
    move_threshold: float = 0.012
    # Per-axis response shape (seeded from motion_model.py; the cerebellum/locomotion uses these). Carried in
    # the profile so a future richer calibration can refine the deadbands/turn-rate per robot.
    forward_deadband: float = 0.25
    turn_deadband: float = 0.08
    turn_min: float = 0.10
    turn_max: float = 0.20
    turn_deg_per_s: float = 90.0
    ts: float = field(default_factory=time.time)
    samples: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_model(cls, model) -> "MotionProfile":
        """A sensible profile seeded entirely from a hard-coded MotionModel (used when uncalibrated)."""
        return cls(
            forward_speed=model.forward_unit_speed, forward_duration=model.forward_unit_duration,
            turn_rx=model.turn_unit_rx, turn_duration=model.turn_unit_duration,
            baseline=round(model.still_diff, 4), move_threshold=round(model.move_diff, 4),
            forward_deadband=model.forward_deadband, turn_deadband=model.turn_deadband,
            turn_min=model.turn_min, turn_max=model.turn_max, turn_deg_per_s=model.turn_deg_per_s,
        )


def load() -> Optional[MotionProfile]:
    try:
        if PROFILE_PATH.is_file():
            return MotionProfile(**json.loads(PROFILE_PATH.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        pass
    return None


def is_calibrated() -> bool:
    return load() is not None


def _save(p: MotionProfile) -> None:
    try:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROFILE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(p.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, PROFILE_PATH)
    except Exception as e:  # noqa: BLE001
        print(f"[calibrate] save failed: {e}", flush=True)


async def _snap(link: RobotLink) -> Optional[bytes]:
    try:
        jpeg, _ = await link.snapshot()
        return jpeg
    except Exception:  # noqa: BLE001
        return None


async def _baseline(link: RobotLink, n: int = 5) -> tuple[float, Optional[bytes]]:
    """Median frame-diff across consecutive still frames = the scene/compression noise floor."""
    frames: list[bytes] = []
    for _ in range(n):
        f = await _snap(link)
        if f:
            frames.append(f)
        await asyncio.sleep(0.25)
    diffs = [d for a, b in zip(frames, frames[1:]) if (d := frame_diff(a, b)) is not None]
    diffs.sort()
    base = diffs[len(diffs) // 2] if diffs else 0.006
    return base, (frames[-1] if frames else None)


async def calibrate(link: RobotLink, *, max_speed: float = 0.85,
                    emit: Optional[Callable[[dict], Awaitable[None]]] = None) -> dict:
    """Run the calibration grid and persist a MotionProfile. Returns the profile dict (or an error).
    The robot DRIVES during this — run it in open space; the caller stops + restores afterward."""
    async def say(msg: str, **extra):
        if emit:
            try:
                await emit({"type": "calibrate", "msg": msg, **extra, "ts": time.time()})
            except Exception:  # noqa: BLE001
                pass

    if await _snap(link) is None:
        return {"ok": False, "error": "no camera frame — is the robot connected/awake?"}

    await say("measuring still baseline...")
    baseline, _ = await _baseline(link)
    move_gate = max(0.012, baseline * 4.0)
    samples: list[dict] = []

    async def trial(kind: str, ly: float, rx: float, dur: float) -> dict:
        before = await _snap(link)
        await link.move(min(ly, max_speed), max(-max_speed, min(max_speed, rx)), dur)
        await asyncio.sleep(dur + _SETTLE)
        await link.stop()
        after = await _snap(link)
        fd = frame_diff(before, after) if (before and after) else None
        res = classify_motion(fd, expected=("translate" if kind == "forward" else "rotate"),
                              baseline=baseline)
        rec = {"kind": kind, "ly": ly, "rx": rx, "duration": dur,
               "frame_diff": fd, "state": res.state}
        samples.append(rec)
        await say(f"{kind} speed={ly or rx} dur={dur}s -> {res.state} (diff={fd})", **rec)
        return rec

    # Forward sweep: pick the smallest burst that reliably MOVED (a controlled step, not a lunge).
    fwd_moved = []
    for sp, dur in _FWD_GRID:
        r = await trial("forward", sp, 0.0, dur)
        if r["state"] == "moved":
            fwd_moved.append((sp, dur))
    fwd = min(fwd_moved, key=lambda x: x[1]) if fwd_moved else (_FWD_GRID[-1][0], _FWD_GRID[-1][1])

    # Turn sweep: smallest burst that clearly changed the view.
    turn_moved = []
    for rx, dur in _TURN_GRID:
        r = await trial("turn", 0.0, rx, dur)
        if r["state"] in ("moved", "blocked"):
            turn_moved.append((rx, dur))
    turn = min(turn_moved, key=lambda x: x[1]) if turn_moved else (_TURN_GRID[-1][0], _TURN_GRID[-1][1])

    prof = MotionProfile(forward_speed=fwd[0], forward_duration=fwd[1],
                         turn_rx=turn[0], turn_duration=turn[1],
                         baseline=round(baseline, 4), move_threshold=round(move_gate, 4),
                         samples=samples)
    _save(prof)
    await say("calibration complete", profile=prof.to_dict())
    moved_any = any(s["state"] == "moved" for s in samples)
    return {"ok": True, "moved_detected": moved_any, "profile": prof.to_dict()}
