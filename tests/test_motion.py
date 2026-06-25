"""Motion-confirmation primitives (shared by the live self-test and the brain's closed loop)."""
from __future__ import annotations

import pytest

from autobot.diagnostics.motion import classify_motion, frame_diff, pose_delta


def test_pose_delta_distance_and_yaw():
    a = {"pose": {"x": 0.0, "y": 0.0, "yaw_deg": 0.0}}
    b = {"pose": {"x": 3.0, "y": 4.0, "yaw_deg": 10.0}, "frames": 7}
    pd = pose_delta(a, b)
    assert abs(pd["dist"] - 5.0) < 1e-6
    assert abs(pd["dyaw_deg"] - 10.0) < 1e-6
    assert pd["frames"] == 7


def test_pose_delta_wraps_yaw():
    a = {"pose": {"x": 0, "y": 0, "yaw_deg": 350}}
    b = {"pose": {"x": 0, "y": 0, "yaw_deg": 10}}
    assert abs(pose_delta(a, b)["dyaw_deg"] - 20.0) < 1e-6


def test_pose_delta_none_without_pose():
    assert pose_delta(None, {"pose": {}}) is None
    assert pose_delta({"pose": {"x": 0, "y": 0, "yaw_deg": 0}}, {}) is None


def test_classify_moved_by_frame_diff():
    assert classify_motion(0.2, expected="translate").state == "moved"


def test_classify_stuck_when_view_unchanged():
    assert classify_motion(0.001, expected="translate").state == "stuck"


def test_classify_blocked_partial_change():
    # between still (0.006) and move (0.012) thresholds => obstructed/partial
    assert classify_motion(0.009, expected="translate").state == "blocked"


def test_classify_unknown_without_frame():
    assert classify_motion(None).state == "unknown"


def test_vslam_pose_never_creates_false_motion():
    # The crux: VSLAM keeps "updating" even when the robot is still. A static camera view must read STUCK
    # regardless of how much the (untrusted) pose claims to have moved.
    m = classify_motion(0.001, pose={"dist": 1.0, "dyaw_deg": 90.0}, expected="translate")
    assert m.state == "stuck"


def test_baseline_raises_the_move_gate():
    # A noisy scene (high still-baseline) demands a bigger change to count as motion.
    assert classify_motion(0.08, baseline=0.05).state != "moved"   # 0.08 < 0.05*2.5
    assert classify_motion(0.30, baseline=0.05).state == "moved"   # clearly beats the noise floor


def test_frame_diff_real_images():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    black = np.zeros((48, 64, 3), dtype=np.uint8)
    white = np.full((48, 64, 3), 255, dtype=np.uint8)
    jb = cv2.imencode(".jpg", black)[1].tobytes()
    jw = cv2.imencode(".jpg", white)[1].tobytes()
    assert frame_diff(jb, jb) < 0.01            # identical -> ~0
    assert frame_diff(jb, jw) > 0.5             # black vs white -> large
