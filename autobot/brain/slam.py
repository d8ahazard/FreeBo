"""VisualSlam — a lightweight visual-(inertial)-odometry consumer of the live video stream.

This is deliberately a *consumer* of `MediaHub`, not part of the decode path: it subscribes to video frames,
buffers them in a small bounded queue, and does all the heavy OpenCV work on its own worker thread. If it
falls behind it drops frames — the camera stream and the brain never stall waiting on SLAM.

What it does (monocular + IMU-assisted):
  * ORB features per frame, matched frame-to-frame, essential-matrix pose recovery -> incremental rotation +
    (unit-scale) translation, integrated into a rough world pose. Monocular scale is unobservable, so the
    map is metric-up-to-scale; the IMU hook lets us refine rotation and (later) recover scale.
  * Keyframes (pose + ORB descriptors) are stored so the robot can relocalize / recognize "I've been here".
  * `add_imu(ts, accel, gyro)` fuses the Air 2's 6-axis IMU: gyro integrates orientation between frames
    (more reliable than vision during fast turns / low-texture views). This is the hook for full VI fusion.
  * `current_xy()` / `pose()` give the spatial system (remember_thing / where_is) a rough coordinate to tag
    objects with, so "where's my charger?" can resolve to a place + bearing later.

Everything OpenCV/NumPy is imported lazily; with neither present the class is inert (enabled=False) and the
rest of FreeBo runs unaffected — same graceful-degradation contract as the other optional skills.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Keyframe:
    seq: int
    wall_ts: float
    pose: tuple                      # (x, y, z, yaw) rough world pose at capture
    descriptors: Any                 # ORB descriptors (for relocalization)
    keypoints: int = 0


@dataclass
class Pose:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    yaw: float = 0.0                 # radians, integrated heading
    updated: float = field(default_factory=time.monotonic)


class VisualSlam:
    # Monocular translation is unit-length per frame (scale is unobservable). Damp it to a nominal step so the
    # integrated track stays in a sane range; absolute scale isn't meaningful, only relative layout is.
    STEP_SCALE = 0.1

    # --- robustness gates (this robot spins fast with no IMU, so vision-only VO must fail SAFE) ---
    FOV_H_DEG = 130.0          # Air 2 camera is wide/fisheye; sets focal length + phase-corr px->yaw mapping
    MAX_DYAW_RAD = math.radians(35.0)   # reject/clamp impossible per-frame yaw jumps (the 300deg-drift bug)
    STILL_GATE = 0.010         # normalized frame-diff below this => robot is stationary; FREEZE pose (no drift)
    MIN_INLIERS = 18           # recoverPose inliers required to trust a vision pose update
    MIN_INLIER_RATIO = 0.45    # inliers / matches required (rejects garbage essential matrices)
    PC_WIDTH = 240             # downscale width for phase-correlation yaw
    PC_CONF_MIN = 0.20         # phase-correlation confidence to trust its yaw (small in-place turns)
    MAX_DT = 1.5               # if frames are >this far apart, skip pose integration (too much could change)

    def __init__(self, *, max_queue: int = 4, max_keyframes: int = 400,
                 keyframe_min_dist: float = 0.5, keyframe_min_yaw: float = 0.25,
                 keyframe_min_frame_gap: int = 8) -> None:
        self.enabled = False
        self._cv2 = None
        self._np = None
        try:
            import cv2  # noqa: F401
            import numpy as np  # noqa: F401
            self._cv2 = cv2
            self._np = np
            self.enabled = True
        except Exception:  # noqa: BLE001
            return

        self._orb = self._cv2.ORB_create(nfeatures=800)
        self._matcher = self._cv2.BFMatcher(self._cv2.NORM_HAMMING, crossCheck=True)
        self._q: deque = deque(maxlen=max_queue)   # bounded: drops oldest frames under load
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._running = False
        self._worker: Optional[threading.Thread] = None

        self._pose = Pose()
        self._prev_gray = None
        self._prev_kp = None
        self._prev_des = None
        self.keyframes: list[Keyframe] = []
        self._max_kf = max_keyframes
        self._kf_min_dist = keyframe_min_dist
        self._kf_min_yaw = keyframe_min_yaw
        self._kf_min_gap = keyframe_min_frame_gap
        self._last_kf_frame = -9999

        # Camera intrinsics from the (wide/fisheye) horizontal FOV instead of a blind 0.8*width pinhole — the
        # Air 2 lens is ~130deg, so the focal length is much shorter; this conditions the essential matrix far
        # better. Monocular pose is still up-to-scale.
        self._K = None

        # IMU buffer for visual-inertial fusion (gyro-integrated yaw bridges low-texture / fast-turn gaps).
        self._imu: deque = deque(maxlen=2000)
        self.frames_processed = 0
        self.last_error: Optional[str] = None
        self._last_conf = 0.0          # last vision-pose confidence (0..1), surfaced for downstream gating
        self._last_int_ts: Optional[float] = None

    # ---- attach to the media stream ----
    def attach(self, hub) -> None:
        """Subscribe to the MediaHub's video. Returns immediately; processing runs on the worker thread."""
        if not self.enabled:
            return
        self._running = True
        self._worker = threading.Thread(target=self._loop, name="visual-slam", daemon=True)
        self._worker.start()
        hub.subscribe_video(self._on_frame)

    def stop(self) -> None:
        self._running = False
        with self._cv:
            self._cv.notify_all()

    def _on_frame(self, frame) -> None:
        # cheap: just enqueue (drop-oldest via deque maxlen). Heavy work happens on the worker.
        if frame.bgr is None:
            return
        with self._cv:
            self._q.append(frame)
            self._cv.notify()

    # ---- IMU fusion hook ----
    def add_imu(self, ts: float, accel: tuple, gyro: tuple) -> None:
        """Feed a 6-axis IMU sample (accel m/s^2, gyro rad/s). Used to integrate yaw between frames."""
        if not self.enabled:
            return
        with self._lock:
            self._imu.append((ts, accel, gyro))

    def _imu_yaw_delta(self, t0: float, t1: float) -> Optional[float]:
        """Integrate gyro-z over [t0, t1] for a yaw increment (robot turns mostly about vertical)."""
        with self._lock:
            samples = [s for s in self._imu if t0 <= s[0] <= t1]
        if len(samples) < 2:
            return None
        yaw = 0.0
        for (ta, _a, ga), (tb, _b, _gb) in zip(samples, samples[1:]):
            yaw += ga[2] * (tb - ta)
        return yaw

    # ---- worker ----
    def _loop(self) -> None:
        cv2, np = self._cv2, self._np
        while self._running:
            with self._cv:
                while self._running and not self._q:
                    self._cv.wait(timeout=1.0)
                if not self._running:
                    break
                frame = self._q.pop()
                self._q.clear()  # latest-only: SLAM cares about the freshest view, not a backlog
            try:
                self._process(frame, cv2, np)
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"

    def _gray_diff(self, a, b, cv2, np) -> Optional[float]:
        """Normalized (0..1) mean-abs-diff of two grayscale frames at 64x64 — the cheap 'is it moving' gate."""
        try:
            ga = cv2.resize(a, (64, 64), interpolation=cv2.INTER_AREA).astype("float32")
            gb = cv2.resize(b, (64, 64), interpolation=cv2.INTER_AREA).astype("float32")
            return float(np.mean(np.abs(ga - gb)) / 255.0)
        except Exception:  # noqa: BLE001
            return None

    def _phase_yaw(self, prev_gray, gray, cv2, np) -> Optional[tuple[float, float]]:
        """(yaw_rad, confidence) from phase-correlation horizontal shift — robust for small in-place pivots,
        which is most of this robot's motion. None on failure."""
        try:
            w = self.PC_WIDTH
            h = max(1, int(gray.shape[0] * w / gray.shape[1]))
            a = cv2.resize(prev_gray, (w, h), interpolation=cv2.INTER_AREA).astype("float32")
            b = cv2.resize(gray, (w, h), interpolation=cv2.INTER_AREA).astype("float32")
            win = cv2.createHanningWindow((w, h), cv2.CV_32F)
            (sx, _sy), resp = cv2.phaseCorrelate(a, b, win)
            yaw = (float(sx) / w) * math.radians(self.FOV_H_DEG)
            return yaw, float(resp)
        except Exception:  # noqa: BLE001
            return None

    def _process(self, frame, cv2, np) -> None:
        gray = frame.gray()
        if gray is None:
            return
        if self._K is None:
            # focal length from horizontal FOV: f = (width/2) / tan(FOV/2). Far shorter than 0.8*width on a
            # wide lens, which is what makes the essential matrix behave.
            f = (frame.width / 2.0) / math.tan(math.radians(self.FOV_H_DEG) / 2.0)
            self._K = np.array([[f, 0, frame.width / 2.0], [0, f, frame.height / 2.0], [0, 0, 1]], dtype=np.float64)

        # FREEZE-ON-STILL: if the view barely changed, the robot is stationary — do NOT integrate VO noise
        # (this is the root cause of the "300deg drift while parked" failure). Keep prev for the next compare.
        if self._prev_gray is not None:
            still = self._gray_diff(self._prev_gray, gray, cv2, np)
            if still is not None and still < self.STILL_GATE:
                self._prev_gray = gray
                self._last_conf = 0.0
                return

        kp, des = self._orb.detectAndCompute(gray, None)
        if des is None or len(kp) < 8:
            self._prev_gray, self._prev_kp, self._prev_des = gray, kp, des
            return

        if self._prev_gray is not None and self._prev_des is not None and len(self._prev_des) >= 8:
            matches = self._matcher.match(self._prev_des, des)
            if len(matches) >= 12:
                pts0 = np.float32([self._prev_kp[m.queryIdx].pt for m in matches])
                pts1 = np.float32([kp[m.trainIdx].pt for m in matches])
                E, mask = cv2.findEssentialMat(pts1, pts0, self._K, method=cv2.RANSAC, prob=0.999, threshold=1.0)
                if E is not None and E.shape == (3, 3):
                    n, R, t, _m = cv2.recoverPose(E, pts1, pts0, self._K, mask=mask)
                    inliers = int(n)
                    ratio = inliers / max(1, len(matches))
                    # Gate on inliers: a garbage essential matrix (the usual fast-rotation failure) is rejected
                    # rather than integrated. Phase-correlation yaw is computed as a robust cross-check/fallback.
                    pc = self._phase_yaw(self._prev_gray, gray, cv2, np)
                    if inliers >= self.MIN_INLIERS and ratio >= self.MIN_INLIER_RATIO:
                        self._integrate(R, t, frame, np, pc, conf=ratio)
                    elif pc is not None and pc[1] >= self.PC_CONF_MIN:
                        self._integrate_yaw_only(pc[0], frame, conf=pc[1])
                    else:
                        self._last_conf = 0.0

        self._prev_gray, self._prev_kp, self._prev_des = gray, kp, des
        self.frames_processed += 1
        self._maybe_keyframe(frame, des, len(kp))

    def _dyaw(self, vis_yaw: float, pc, frame) -> float:
        """Pick the best per-frame yaw delta and clamp it. Priority: IMU (if present) > confident phase-corr >
        essential-matrix yaw. Clamped to MAX_DYAW_RAD so a single bad frame can't whip the heading around."""
        prev_ts = getattr(self, "_last_frame_ts", None)
        imu_yaw = self._imu_yaw_delta(prev_ts, frame.wall_ts) if prev_ts else None
        self._last_frame_ts = frame.wall_ts
        if imu_yaw is not None:
            dyaw = imu_yaw
        elif pc is not None and pc[1] >= self.PC_CONF_MIN:
            dyaw = pc[0]
        else:
            dyaw = vis_yaw
        return max(-self.MAX_DYAW_RAD, min(self.MAX_DYAW_RAD, dyaw))

    def _integrate(self, R, t, frame, np, pc=None, conf: float = 0.0) -> None:
        dyaw = self._dyaw(math.atan2(R[2, 0], R[2, 2]), pc, frame)
        # Rotation-dominant frame (a pivot): there's no parallax, so translation from recoverPose is noise —
        # zero it. Detected when phase-corr sees a clear horizontal shift.
        rotation_dominant = pc is not None and pc[1] >= self.PC_CONF_MIN and abs(pc[0]) > math.radians(3.0)
        now = time.monotonic()
        dt_ok = self._last_int_ts is None or (now - self._last_int_ts) <= self.MAX_DT
        self._last_int_ts = now
        with self._lock:
            self._pose.yaw = (self._pose.yaw + dyaw + math.pi) % (2 * math.pi) - math.pi
            if dt_ok and not rotation_dominant:
                step = (float(t[2]) if abs(float(t[2])) > 1e-6 else 0.0) * self.STEP_SCALE
                step = max(-self.STEP_SCALE, min(self.STEP_SCALE, step))   # clamp per-frame translation
                self._pose.x += math.cos(self._pose.yaw) * step
                self._pose.y += math.sin(self._pose.yaw) * step
                self._pose.z += float(t[1]) * self.STEP_SCALE
            self._pose.updated = now
            self._last_conf = round(float(conf), 3)

    def _integrate_yaw_only(self, pc_yaw: float, frame, conf: float = 0.0) -> None:
        """Vision pose was untrustworthy (too few inliers) but phase-correlation gave a confident yaw — update
        heading only, no translation. Typical during a fast in-place pivot."""
        dyaw = max(-self.MAX_DYAW_RAD, min(self.MAX_DYAW_RAD, pc_yaw))
        self._last_frame_ts = frame.wall_ts
        with self._lock:
            self._pose.yaw = (self._pose.yaw + dyaw + math.pi) % (2 * math.pi) - math.pi
            self._pose.updated = time.monotonic()
            self._last_conf = round(float(conf), 3)

    def _maybe_keyframe(self, frame, des, nkp: int) -> None:
        with self._lock:
            p = self._pose
            if self.frames_processed - self._last_kf_frame < self._kf_min_gap:
                return  # don't spawn keyframes every frame — keep the map sparse and well-spaced
            if self.keyframes:
                last = self.keyframes[-1].pose
                moved = math.hypot(p.x - last[0], p.y - last[1])
                turned = abs((p.yaw - last[3] + math.pi) % (2 * math.pi) - math.pi)
                if moved < self._kf_min_dist and turned < self._kf_min_yaw:
                    return
            self._last_kf_frame = self.frames_processed
            self.keyframes.append(Keyframe(seq=frame.seq, wall_ts=frame.wall_ts,
                                           pose=(p.x, p.y, p.z, p.yaw), descriptors=des, keypoints=nkp))
            if len(self.keyframes) > self._max_kf:
                self.keyframes.pop(0)

    # ---- queries (for the spatial / places system) ----
    def pose(self) -> dict:
        with self._lock:
            p = self._pose
            return {"x": round(p.x, 3), "y": round(p.y, 3), "z": round(p.z, 3),
                    "yaw_deg": round(math.degrees(p.yaw), 1), "keyframes": len(self.keyframes),
                    "frames": self.frames_processed, "enabled": self.enabled,
                    "confidence": self._last_conf}

    def current_xy(self) -> tuple:
        with self._lock:
            return (self._pose.x, self._pose.y, self._pose.yaw)

    def map_data(self) -> dict:
        """Everything the UI minimap needs: current pose + the keyframe trail (rough world XY path)."""
        with self._lock:
            p = self._pose
            trail = [[round(kf.pose[0], 3), round(kf.pose[1], 3)] for kf in self.keyframes]
            return {
                "enabled": self.enabled,
                "pose": {"x": round(p.x, 3), "y": round(p.y, 3),
                         "yaw_deg": round(math.degrees(p.yaw), 1)},
                "trail": trail,
                "keyframes": len(self.keyframes),
                "frames": self.frames_processed,
                "confidence": self._last_conf,
            }

    def relocalize(self, frame, min_inliers: int = 30) -> Optional[Keyframe]:
        """Best-matching stored keyframe for a frame's view — 'have I been here before?'."""
        if not self.enabled:
            return None
        gray = frame.gray()
        if gray is None:
            return None
        _kp, des = self._orb.detectAndCompute(gray, None)
        if des is None:
            return None
        best, best_n = None, min_inliers
        with self._lock:
            kfs = list(self.keyframes)
        for kf in kfs:
            if kf.descriptors is None:
                continue
            try:
                m = self._matcher.match(kf.descriptors, des)
            except Exception:  # noqa: BLE001
                continue
            if len(m) > best_n:
                best, best_n = kf, len(m)
        return best
