"""Vision reflex (brainstem) — a fast, non-LLM "something is looming, STOP" signal from the camera alone.

The EBO Air 2 exposes no ToF/IR distance in its cloud stream, so the original `agent._reflex_loop` ToF reflex
never fires there (docs/MOTION.md). This restores a reflex using only the camera: when the robot drives
toward a near obstacle, the optical-flow field EXPANDS (diverges) outward from the image center. Crucially,
divergence isolates *approach* from *turning* — a pure in-place pivot produces translational flow with ~zero
divergence, so this does not false-fire on turns.

Cheap (sparse Lucas-Kanade on a small grayscale frame), optional cv2 (returns 0 when unavailable), and
self-gating (≈0 when still). It is a best-effort guard between decisions, not a precise rangefinder; the
cerebellum's per-step camera confirmation (locomotion.py) remains the primary protection.
"""
from __future__ import annotations

import os
from typing import Optional

# Tunables (env-overridable). Threshold is normalized mean radial expansion per frame; conservative so it
# rarely false-fires. 0 disables. Enabled by default only for robots without real proximity sensing.
LOOM_THRESHOLD = float(os.environ.get("AUTOBOT_LOOM_THRESHOLD", "0.018"))
_WIDTH = 192
_CENTER_FRAC = 0.7   # only count features in the central region (where a head-on obstacle looms)


class LoomingDetector:
    """Feed it consecutive JPEG frames; get back a looming score (>= 0). Higher = expanding toward something."""

    def __init__(self) -> None:
        self._prev = None
        self._ok = True

    def _gray(self, jpeg: bytes):
        try:
            import cv2
            import numpy as np
            arr = np.frombuffer(jpeg, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
            if img is None:
                return None
            h = max(1, int(img.shape[0] * _WIDTH / img.shape[1]))
            return cv2.resize(img, (_WIDTH, h), interpolation=cv2.INTER_AREA)
        except Exception:  # noqa: BLE001
            self._ok = False
            return None

    def update(self, jpeg: Optional[bytes]) -> float:
        if not jpeg or not self._ok:
            return 0.0
        try:
            import cv2
            import numpy as np
        except Exception:  # noqa: BLE001
            self._ok = False
            return 0.0
        gray = self._gray(jpeg)
        if gray is None:
            return 0.0
        prev = self._prev
        self._prev = gray
        if prev is None or prev.shape != gray.shape:
            return 0.0
        try:
            feats = cv2.goodFeaturesToTrack(prev, maxCorners=120, qualityLevel=0.01, minDistance=6)
            if feats is None or len(feats) < 8:
                return 0.0
            nxt, st, _err = cv2.calcOpticalFlowPyrLK(prev, gray, feats, None)
            if nxt is None or st is None:
                return 0.0
            h, w = gray.shape
            cx, cy = w / 2.0, h / 2.0
            rx, ry = w * _CENTER_FRAC / 2.0, h * _CENTER_FRAC / 2.0
            radials = []
            for (p0, p1, ok) in zip(feats.reshape(-1, 2), nxt.reshape(-1, 2), st.reshape(-1)):
                if not ok:
                    continue
                dx0, dy0 = float(p0[0] - cx), float(p0[1] - cy)
                if abs(dx0) > rx or abs(dy0) > ry:
                    continue   # outside the central region — ignore edge flow
                dist = (dx0 * dx0 + dy0 * dy0) ** 0.5
                if dist < 4.0:
                    continue
                vx, vy = float(p1[0] - p0[0]), float(p1[1] - p0[1])
                radials.append((vx * dx0 + vy * dy0) / dist)   # outward (expansion) component
            if len(radials) < 6:
                return 0.0
            return max(0.0, float(np.mean(radials)) / w)
        except Exception:  # noqa: BLE001
            return 0.0
