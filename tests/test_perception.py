"""Perception: telemetry + snapshot -> Observation, and its text summary for the model."""
from __future__ import annotations

from autobot.brain.perception import Observation, perceive
from autobot.robot.mock_link import MockRobotLink


def test_text_summary_disconnected():
    o = Observation(telemetry={"connected": False, "awake": False})
    assert "DISCONNECTED" in o.text_summary()


def test_text_summary_battery_eyes_and_camera():
    o = Observation(telemetry={"connected": True, "awake": True, "battery": 73, "eyes_animation": "happy"},
                    jpeg=b"\xff\xd8\xff\xd9")
    s = o.text_summary()
    assert "battery 73%" in s and "eyes: happy" in s and "camera frame attached" in s


def test_text_summary_resting_and_touched():
    o = Observation(telemetry={"connected": True, "awake": True, "resting": True, "touched": True})
    s = o.text_summary()
    assert "RESTING" in s and "TOUCHED" in s


def test_image_data_url():
    o = Observation(jpeg=b"\xff\xd8\xff")
    assert o.has_image
    assert o.image_data_url().startswith("data:image/jpeg;base64,")
    assert Observation().image_data_url() is None


async def test_perceive_from_mock_link():
    obs = await perceive(MockRobotLink(), want_image=True)
    assert obs.telemetry.get("connected") is True
    assert obs.has_image  # mock returns a (synthetic) frame when awake
