"""Camera-only motion sensing — the robot's substitute for the proprioception/odometry it doesn't have.

On the EBO Air 2 there is no IMU, no wheel odometry, and no usable ToF, and monocular VSLAM drifts badly.
What IS reliable is the camera itself: if the robot physically moved, the view changes; an in-place yaw shows
up as a clean horizontal image shift. This module turns two frames into objective numbers:

  * `diff`        — mean abs pixel difference (0..1): gross "did anything move" (robust, used everywhere).
  * `shift_x/y`   — phase-correlation pixel shift: a small in-place yaw is a horizontal shift (only reliable
                    for SMALL motions; big rotations decorrelate -> low confidence).
  * `est_yaw_deg` — shift_x mapped through the (wide/fisheye) horizontal FOV to rough degrees (relative).

Pure functions, optional cv2/numpy, fail-soft (return None / empty). Shared by locomotion (the cerebellum),
the vision reflex, VSLAM hardening, motion_check, and the /api/overseer/probe endpoint.
"""
from __future__ import annotations

from typing import Optional

# Re-export the canonical frame-diff so callers have one import for all camera-motion math.
from ..diagnostics.motion import frame_diff  # noqa: F401

# Air 2 camera is heavily fisheye/wide; ~130deg horizontal FOV is a good working value for px->deg.
DEFAULT_FOV_H = 130.0


def gray_small(jpeg: bytes, width: int = 320):
    """Decode a JPEG to a float32 grayscale array ~`width` px wide (aspect kept). None on failure/no cv2."""
    if not jpeg:
        return None
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        h = max(1, int(img.shape[0] * width / img.shape[1]))
        return cv2.resize(img, (width, h), interpolation=cv2.INTER_AREA).astype("float32")
    except Exception:  # noqa: BLE001
        return None


def phase_shift(ga, gb) -> Optional[tuple[float, float, float]]:
    """(shift_x, shift_y, confidence) between two equal-size float32 gray arrays via phase correlation.
    confidence ~1.0 for a clean small shift, ->0 when frames decorrelate (large/blurred motion). None if cv2
    is missing or shapes mismatch."""
    if ga is None or gb is None or ga.shape != gb.shape:
        return None
    try:
        import cv2
        win = cv2.createHanningWindow((ga.shape[1], ga.shape[0]), cv2.CV_32F)
        (sx, sy), resp = cv2.phaseCorrelate(ga, gb, win)
        return float(sx), float(sy), float(resp)
    except Exception:  # noqa: BLE001
        return None


def measure(before_jpeg: Optional[bytes], after_jpeg: Optional[bytes], *,
            fov_h: float = DEFAULT_FOV_H, width: int = 320) -> dict:
    """Full camera-motion measurement between two JPEG frames. Keys: ok, diff, shift_x, shift_y, confidence,
    est_yaw_deg (any may be absent if undecodable). This is the shared core of /api/overseer/probe."""
    out: dict = {"ok": False}
    ga = gray_small(before_jpeg, width) if before_jpeg else None
    gb = gray_small(after_jpeg, width) if after_jpeg else None
    if ga is None or gb is None:
        return out
    try:
        import numpy as np
        out["diff"] = round(float(np.mean(np.abs(ga - gb)) / 255.0), 4)
    except Exception:  # noqa: BLE001
        pass
    ps = phase_shift(ga, gb)
    if ps is not None:
        sx, sy, conf = ps
        out["shift_x"] = round(sx, 2)
        out["shift_y"] = round(sy, 2)
        out["confidence"] = round(conf, 3)
        out["est_yaw_deg"] = round(sx / ga.shape[1] * fov_h, 1)
    out["ok"] = "diff" in out
    return out


def yaw_between(before_jpeg: Optional[bytes], after_jpeg: Optional[bytes], *,
                fov_h: float = DEFAULT_FOV_H) -> Optional[tuple[float, float]]:
    """(est_yaw_deg, confidence) for an in-place pivot, or None. Convenience over measure()."""
    m = measure(before_jpeg, after_jpeg, fov_h=fov_h)
    if "est_yaw_deg" in m:
        return m["est_yaw_deg"], m.get("confidence", 0.0)
    return None
