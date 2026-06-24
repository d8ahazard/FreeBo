#!/usr/bin/env python3
"""Phase 0 motion-acceptance harness.

TWO clearly-separated modes:

  * OFFLINE (default) — a SIMULATION that exercises the ActionExecutor against the MockRobotLink: lifecycle
    transitions, stale-frame -> UNKNOWN (never STUCK), cancellation -> CANCELLED, oscillation -> HOLD within
    the breaker threshold, and execution-latency instrumentation. It does NOT and CANNOT claim "zero
    collisions" — there is no physical environment or collision model here. Collision-free results require the
    hardware course below.

  * HARDWARE (`--hardware`) — the physical 50-step obstacle course. It moves a REAL robot, so it is gated:
    it refuses to run unless BOTH `--hardware` AND `AUTOBOT_COURSE_ENABLE_MOTION=1` are set, and it prints the
    test contract (speed, layout, lighting, operator stop, collision definition) it expects the operator to
    follow. Wiring it to a live app is intentionally left to the operator runbook in docs/TEST_PLAN.md.

    python scripts/obstacle_course.py                 # offline simulation (CI-safe)
    python scripts/obstacle_course.py --steps 50      # more simulated steps
    AUTOBOT_COURSE_ENABLE_MOTION=1 python scripts/obstacle_course.py --hardware
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _run_offline(steps: int) -> int:
    import time as _t

    from autobot.brain.action_executor import ActionExecutor, State
    from autobot.brain.metrics import Metrics
    from autobot.brain.safety import SafetyFloor
    from autobot.config import Settings
    from autobot.robot.media_hub import FrameSample
    from autobot.robot.mock_link import MockRobotLink

    def _sim_jpeg(level: int):
        """A small VALID JPEG at a given brightness, so consecutive distinct frames read as a real 'moved'
        (frame-diff above the gate). Falls back to None if no imaging lib (then frames are identical)."""
        try:
            import cv2
            import numpy as np
            img = np.full((48, 64, 3), level % 256, dtype=np.uint8)
            ok, buf = cv2.imencode(".jpg", img)
            return buf.tobytes() if ok else None
        except Exception:  # noqa: BLE001
            return None

    class _SimLink(MockRobotLink):
        """A mock whose camera visibly CHANGES each move (so motion reads 'moved'), unless `_freeze_seq` is
        set (stalled stream — same seq -> the executor must read UNKNOWN)."""
        def __init__(self):
            super().__init__()
            self._lvl = 0

        async def snapshot_sample(self):
            if not self._freeze_seq:
                self._seq += 1
                self._lvl += 80
            jpeg = _sim_jpeg(self._lvl) or self._snapshot_bytes()
            return FrameSample(jpeg=jpeg, seq=self._seq, wall_ts=_t.monotonic(), age=0.0, valid=True)

    s = Settings()
    s.update(autonomy="auto", allow_motion=True, max_speed=0.6)
    metrics = Metrics()
    sf = SafetyFloor()
    results = {"succeeded": 0, "unknown": 0, "failed": 0, "cancelled": 0}
    checks: list[tuple[str, bool]] = []

    def _ex(link, **kw):
        params = {"evidence_timeout": 0.4, "settle": 0.02, "poll": 0.01, "metrics": metrics}
        params.update(kw)
        return ActionExecutor(link, sf, **params)

    async def pulse(ex, link, ly, rx, source="ai", parent_id=None):
        sf.begin_tick()   # one reason cycle per pulse (resets the per-tick rate limiter, as the agent does)
        return await ex.run_drive(ly, rx, 0.3, settings=s, source=source, parent_id=parent_id)

    # 1) Normal pulses on a moving view -> SUCCEEDED('moved'); collect latency + lifecycle.
    link1 = _SimLink(); ex1 = _ex(link1)
    moved = 0
    for _ in range(max(1, steps)):
        a = await pulse(ex1, link1, 0.4, 0.0)
        results[a.state.value] = results.get(a.state.value, 0) + 1
        moved += int(a.result == "moved")
    checks.append(("normal pulses reach a terminal state", sum(results.values()) >= steps))
    checks.append(("a moving view reads 'moved' (no false stuck)", moved >= 1))

    # 2) Stale stream -> UNKNOWN, never STUCK.
    link2 = _SimLink(); ex2 = _ex(link2)
    await link2.snapshot_sample(); link2._freeze_seq = True
    a = await pulse(ex2, link2, 0.4, 0.0)
    checks.append(("stale frame -> UNKNOWN (not stuck)", a.state == State.UNKNOWN and a.result == "unknown"))

    # 3) Oscillation -> HOLD within the breaker threshold (two consecutive non-progress attempts).
    link3 = _SimLink(); ex3 = _ex(link3)
    await link3.snapshot_sample(); link3._freeze_seq = True
    a = await pulse(ex3, link3, 0.4, 0.0)
    a = await pulse(ex3, link3, 0.0, 0.4, source="recovery", parent_id=a.id)
    checks.append(("oscillation -> HOLD within 2 attempts", ex3.in_hold()))
    refused = await pulse(ex3, link3, 0.4, 0.0)
    checks.append(("HOLD refuses further AI motion", refused.state == State.FAILED
                   and "circuit breaker" in refused.reason))

    # 4) Cancellation -> CANCELLED (not FAILED).
    link4 = _SimLink(); ex4 = _ex(link4, evidence_timeout=1.0, settle=0.3)
    sf.begin_tick()
    task = asyncio.create_task(ex4.run_drive(0.4, 0.0, 0.3, settings=s, source="ai"))
    await asyncio.sleep(0.05)
    await ex4.preempt("test cancel")
    ca = await task
    checks.append(("preempt -> CANCELLED (not FAILED)", ca.state == State.CANCELLED))

    # --- report ---
    exec_stats = metrics.summary().get("execute", {})
    print("=== Phase 0 OFFLINE simulation (NO physical collision model) ===")
    print(f"steps={steps}  lifecycle={results}")
    if exec_stats:
        print(f"execute latency ms: p50={exec_stats.get('p50', 0):.1f} "
              f"p95={exec_stats.get('p95', 0):.1f} max={exec_stats.get('max', 0):.1f}")
    print("checks:")
    ok = True
    for name, passed in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("NOTE: 'zero collisions' is a HARDWARE-course gate; this run makes no collision claim.")
    return 0 if ok else 1


def _run_hardware() -> int:
    if os.environ.get("AUTOBOT_COURSE_ENABLE_MOTION") != "1":
        print("REFUSED: the physical obstacle course moves a real robot.")
        print("Set BOTH --hardware AND AUTOBOT_COURSE_ENABLE_MOTION=1 to enable motion.")
        print()
        print("Test contract (see docs/TEST_PLAN.md):")
        print("  - tested speed:      config.max_speed (record the value used)")
        print("  - course layout:     50 marked steps, obstacles at fixed positions (document the map)")
        print("  - lighting:          steady, documented lux (the cloud camera is noise-sensitive)")
        print("  - operator stop:     a human with the UI STOP / physical power within reach at all times")
        print("  - collision defn:    any contact with an obstacle or wall = a collision")
        print("  - pass gate:         0 collisions over the 50 steps at the tested speed + environment")
        return 2
    print("Hardware course motion enabled. Wire this to your running app per docs/TEST_PLAN.md.")
    print("(Live-driving wiring is intentionally operator-owned and not auto-run here.)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0 motion-acceptance harness")
    ap.add_argument("--hardware", action="store_true", help="physical obstacle course (gated; moves a real robot)")
    ap.add_argument("--steps", type=int, default=12, help="simulated pulses in offline mode")
    args = ap.parse_args()
    if args.hardware:
        return _run_hardware()
    return asyncio.run(_run_offline(args.steps))


if __name__ == "__main__":
    raise SystemExit(main())
