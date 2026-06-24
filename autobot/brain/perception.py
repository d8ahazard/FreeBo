"""Perception: turn the robot link's telemetry + a camera snapshot into an Observation for the model.

The Observation carries a structured telemetry dict, the JPEG bytes (if any), and helpers to render a
compact text summary and an OpenAI image content part (data URL). See docs/AI_BRAIN.md.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field

from ..robot.link import RobotLink
from ..robot.media_hub import FrameSample


@dataclass
class Observation:
    telemetry: dict = field(default_factory=dict)
    jpeg: bytes | None = None
    snapshot_error: str | None = None
    ts: float = field(default_factory=time.time)
    caption: str = ""    # set in hybrid mode: a vision model's description of the frame (for a text-only brain)
    # Sequence-aware frame sample (seq/wall_ts/age/valid). `jpeg` is retained as a plain field for
    # compatibility (Phase 0.6); motion evidence reads `frame.seq` to require a NEW frame after a pulse.
    frame: FrameSample | None = None

    @property
    def has_image(self) -> bool:
        return bool(self.jpeg)

    def image_data_url(self) -> str | None:
        if not self.jpeg:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(self.jpeg).decode()

    def text_summary(self) -> str:
        t = self.telemetry
        if not t.get("ok", True) and "error" in t:
            return f"Robot telemetry unavailable ({t.get('error')}). Assume stopped."
        parts = []
        conn = t.get("connected")
        awake = t.get("awake")
        parts.append("connected" if conn else "DISCONNECTED")
        if t.get("paused"):
            parts.append("session released to the app")
        parts.append("awake" if awake else "asleep")
        batt = t.get("battery", -1)
        if isinstance(batt, (int, float)) and batt >= 0:
            chg = " (charging)" if t.get("charge") == 1 else ""
            parts.append(f"battery {int(batt)}%{chg}")
        tog = t.get("toggles") or {}
        on = [k for k, v in tog.items() if v]
        if on:
            parts.append("enabled: " + ", ".join(sorted(on)))
        if t.get("eyes_animation"):
            parts.append(f"eyes: {t['eyes_animation']}")
        ao = t.get("audio_out") or {}
        if ao.get("available") is False:
            parts.append("talkback unavailable on this unit")
        # Sensor telemetry (6-axis IMU + IR time-of-flight) when the unit reports it.
        tof = t.get("tof", t.get("distance"))
        if isinstance(tof, (int, float)) and tof >= 0:
            parts.append(f"obstacle {int(tof)}cm ahead" if tof < 30 else f"clear ~{int(tof)}cm ahead")
        # Charging/docked. A topped-up battery (>90%) is free to leave the dock and roam, so don't tell the
        # model it "cannot drive" in that case (matches AgentBrain._resting).
        if t.get("resting") or t.get("charge") == 1:
            batt = t.get("battery", -1)
            if isinstance(batt, (int, float)) and batt > 90:
                parts.append("on the dock, fully charged — free to head out")
            else:
                parts.append("RESTING (charging/docked) — cannot drive")
        if t.get("touched"):
            parts.append("⚠ JUST TOUCHED/BUMPED — react to it")
        cam = "camera frame attached" if self.has_image else f"no camera frame ({self.snapshot_error or 'n/a'})"
        return f"Robot status: {', '.join(parts)}. {cam}."


async def perceive(link: RobotLink, want_image: bool = True) -> Observation:
    telemetry = await link.telemetry()
    jpeg = None
    err = None
    frame = None
    if want_image and telemetry.get("awake"):
        frame = await link.snapshot_sample()
        jpeg, err = frame.jpeg, frame.error
    elif want_image:
        err = "asleep"
    return Observation(telemetry=telemetry, jpeg=jpeg, snapshot_error=err, frame=frame)
