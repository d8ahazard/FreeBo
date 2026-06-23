"""Motion confirmation primitives: did the robot actually move?

Pure functions only — no autobot imports, optional cv2/Pillow/numpy (graceful fallback). This is shared by
the live self-test harness (compares snapshots pulled over HTTP) and the brain's closed-loop motion check
(compares frames from the link). Two independent signals are combined:

  * frame-diff  — mean absolute difference of two downscaled grayscale frames (0..1). A real translation or
                  rotation changes most of the view; a wedged/stuck robot does not.
  * pose-delta  — VSLAM pose change (distance + yaw). Monocular + no Air 2 IMU, so this is odometry-grade
                  (relative, up-to-scale), used as corroboration, not ground truth.

`classify_motion()` folds both (plus what was commanded) into one of: moved / stuck / blocked / unknown.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Thresholds (tuned for the Air 2's ~640px frames downscaled to 64x64). Conservative on purpose: we'd rather
# say "unknown" than falsely claim motion. When a per-scene `baseline` (the camera diff while the robot is
# STILL) is supplied, the gates scale off it — far more robust than a fixed number.
# Absolute floors (used when there's no baseline). Measured on a live Air 2: stationary noise ~0.001, a
# forward drive ~0.18, a slow in-place turn ~0.008–0.02 — so the move floor sits well above noise but low
# enough to catch a real turn, and the per-scene baseline (median of several still frames) does the rest.
FRAME_MOVE_THRESHOLD = 0.012   # normalized mean-abs-diff above this => the view changed meaningfully
FRAME_STILL_THRESHOLD = 0.006  # below this => the view is essentially identical (likely didn't move)
BASELINE_MOVE_FACTOR = 4.0     # a real move must change the view this many times more than the still noise
BASELINE_STILL_FACTOR = 2.0    # within this much of the still noise => indistinguishable from not moving


def _decode_gray_small(jpeg: bytes, size: int = 64):
    """Decode JPEG bytes to a small float32 grayscale array (size x size). None if no decoder is available."""
    if not jpeg:
        return None
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        img = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
        return img.astype("float32")
    except Exception:  # noqa: BLE001 — fall through to Pillow
        pass
    try:
        import io

        import numpy as np
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg)).convert("L").resize((size, size))
        return np.asarray(img, dtype="float32")
    except Exception:  # noqa: BLE001
        return None


def frame_diff(a: bytes, b: bytes, size: int = 64) -> Optional[float]:
    """Normalized (0..1) mean absolute difference between two JPEG frames. None if undecodable.

    Falls back to a crude byte-size ratio only when no image library is present, so the caller still gets a
    rough signal rather than nothing (kept clearly separate via the `None`-on-no-decoder contract above)."""
    ga = _decode_gray_small(a, size)
    gb = _decode_gray_small(b, size)
    if ga is None or gb is None:
        if a and b:
            # No imaging lib: very rough proxy from compressed size (more change => more bytes). Bounded 0..1.
            la, lb = len(a), len(b)
            return min(1.0, abs(la - lb) / max(1, max(la, lb)))
        return None
    import numpy as np
    return float(np.mean(np.abs(ga - gb)) / 255.0)


def pose_delta(before: Optional[dict], after: Optional[dict]) -> Optional[dict]:
    """Distance + yaw change between two `/api/slam/map` (or VSLAM.map_data()) payloads. None if no pose."""
    pa = (before or {}).get("pose") or {}
    pb = (after or {}).get("pose") or {}
    if not pa or not pb:
        return None
    dx = float(pb.get("x", 0.0)) - float(pa.get("x", 0.0))
    dy = float(pb.get("y", 0.0)) - float(pa.get("y", 0.0))
    dist = math.hypot(dx, dy)
    dyaw = abs(((float(pb.get("yaw_deg", 0.0)) - float(pa.get("yaw_deg", 0.0)) + 180.0) % 360.0) - 180.0)
    return {"dist": round(dist, 4), "dyaw_deg": round(dyaw, 2),
            "frames": after.get("frames") if isinstance(after, dict) else None,
            "keyframes": after.get("keyframes") if isinstance(after, dict) else None}


@dataclass
class MotionResult:
    state: str                       # moved | stuck | blocked | unknown
    frame_diff: Optional[float] = None
    pose: Optional[dict] = None
    expected: str = "any"            # what we asked for: translate | rotate | any
    detail: str = ""
    evidence: dict = field(default_factory=dict)


def classify_motion(fd: Optional[float], *, expected: str = "any", baseline: Optional[float] = None,
                    pose: Optional[dict] = None, frame_move: float = FRAME_MOVE_THRESHOLD,
                    frame_still: float = FRAME_STILL_THRESHOLD) -> MotionResult:
    """Decide whether the robot PHYSICALLY moved, from the robot's own camera view only.

    The robot's camera is the source of truth: if it actually drove/turned, the view changes a lot; if it
    stayed put, the view is ~static. VSLAM pose is deliberately NOT used to decide — monocular odometry drifts
    and keeps "updating" even when the robot is stationary, which produces false "it moved" passes (the exact
    failure we're guarding against). `pose` is recorded as advisory evidence only.

    `baseline` is the camera diff measured while the robot was STILL (compression + scene noise). When given,
    the move/still gates scale off it so a real move must beat the noise floor, not a fixed constant.

    expected ("translate"|"rotate"|"any") is informational; both translation and rotation change the view.
    """
    ev: dict = {}
    if fd is not None:
        ev["frame_diff"] = round(fd, 4)
    if baseline is not None:
        ev["baseline_diff"] = round(baseline, 4)
    if pose is not None:
        ev["slam_pose"] = pose          # advisory ONLY — never used as proof of motion (it drifts when still)
        ev["slam_note"] = "advisory; VSLAM not trusted for the motion verdict"

    if fd is None:
        return MotionResult("unknown", fd, pose, expected,
                            "no camera frame to compare — cannot verify motion", ev)

    move_gate = max(frame_move, baseline * BASELINE_MOVE_FACTOR) if baseline else frame_move
    still_gate = max(frame_still, baseline * BASELINE_STILL_FACTOR) if baseline else frame_still
    ev["move_gate"] = round(move_gate, 4)

    if fd >= move_gate:
        return MotionResult("moved", fd, pose, expected,
                            f"camera view changed {fd:.3f} (> gate {move_gate:.3f})", ev)
    if fd <= still_gate:
        return MotionResult("stuck", fd, pose, expected,
                            f"camera view essentially unchanged {fd:.3f} — robot did NOT move", ev)
    return MotionResult("blocked", fd, pose, expected,
                        f"only a slight view change {fd:.3f} (< gate {move_gate:.3f}) — partial/obstructed", ev)
