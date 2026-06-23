"""Autobot diagnostics: a live-robot capability self-test harness + reusable check logic.

This package drives the *running app* over its HTTP API (it never opens a second robot session — the
air2_native link owns the only Agora RTM/RTC session) and reports a clear PASS/FAIL/SKIP per capability:
connect, video, move+motion-confirm, rotate, eyes, talk, hear, autonomy, VSLAM.

Layers:
  * `motion`  — pure image/pose helpers (frame-diff + SLAM pose delta + motion classification). No autobot
                imports, optional cv2/Pillow; safe to import from the brain too (closed-loop motion check).
  * `client`  — a thin async HTTP client for the app API.
  * `checks`  — one async function per capability, each returning a `CheckResult`.
  * `runner`  — orders the checks, prints a table / JSON, restores settings, and always stops the robot.

Run it via `python scripts/robot_selftest.py` (see that script for flags).
"""
from __future__ import annotations

from .checks import CheckResult, Status

__all__ = ["CheckResult", "Status"]
