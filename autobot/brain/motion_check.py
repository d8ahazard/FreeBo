"""Closed-loop motion confirmation for the brain: "did my last move actually move me?"

When the agent issues a drive, it records the camera frame (and VSLAM pose, if available) at that instant.
On the next decision cycle it compares against the current frame + pose to classify the outcome
(moved / blocked / stuck / unknown). The brain uses this to react instead of blindly re-issuing a forward
that goes nowhere — and to surface a clear motion state to the UI.

This reuses the same pure primitives as the live self-test (`autobot.diagnostics.motion`), so the offline
harness and the live brain agree on what "moved" means. Everything here is fail-soft: any error yields no
result and never perturbs the agent loop.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from ..diagnostics.motion import MotionResult, classify_motion, frame_diff, pose_delta


class MotionConfirmer:
    def __init__(self, pose_provider: Optional[Callable[[], dict]] = None) -> None:
        # pose_provider returns a `/api/slam/map`-shaped dict ({"pose": {...}, ...}); set by the server when
        # VSLAM is available. None => motion confirmation falls back to camera frame-diff only.
        self.pose_provider = pose_provider
        self._pending: Optional[dict] = None

    def _pose(self) -> Optional[dict]:
        try:
            return self.pose_provider() if self.pose_provider else None
        except Exception:  # noqa: BLE001
            return None

    def record(self, before_jpeg: Optional[bytes], ly: float, rx: float) -> None:
        """Remember the world state at the moment a move is issued."""
        intent = "translate" if abs(float(ly or 0.0)) >= abs(float(rx or 0.0)) else "rotate"
        self._pending = {"jpeg": before_jpeg, "pose": self._pose(), "intent": intent,
                         "ly": ly, "rx": rx, "ts": time.monotonic()}

    def has_pending(self) -> bool:
        return self._pending is not None

    def confirm(self, current_jpeg: Optional[bytes]) -> Optional[MotionResult]:
        """Compare against the current frame/pose and classify. Consumes the pending record."""
        p, self._pending = self._pending, None
        if not p:
            return None
        try:
            fd = frame_diff(p["jpeg"], current_jpeg) if (p.get("jpeg") and current_jpeg) else None
            pd = pose_delta(p.get("pose"), self._pose())
            # Camera frame-diff is the verdict; VSLAM pose is advisory only (it drifts when stationary).
            res = classify_motion(fd, expected=p["intent"], pose=pd)
            res.evidence["intent"] = p["intent"]
            res.evidence["dt"] = round(time.monotonic() - p["ts"], 2)
            return res
        except Exception:  # noqa: BLE001
            return None
