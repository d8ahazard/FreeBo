"""MAVLink frame builders + the robot's parameter/action maps — the single source of truth for the
control protocol (mirrors docs/BRIDGE_PROTOCOL.md). Used by NativeRobotLink to talk to the robot.

Any new control frame/IOCTL must be documented in docs/BRIDGE_PROTOCOL.md in the same change.
"""
from __future__ import annotations

import os
import struct


# ---------------- MAVLink builders (control over RDT) ----------------
def mavlink_crc(data: bytes, crc_extra: int) -> int:
    crc = 0xFFFF
    for b in data:
        t = b ^ (crc & 0xFF); t = (t ^ (t << 4)) & 0xFF
        crc = ((crc >> 8) ^ (t << 8) ^ (t << 3) ^ (t >> 4)) & 0xFFFF
    t = crc_extra ^ (crc & 0xFF); t = (t ^ (t << 4)) & 0xFF
    crc = ((crc >> 8) ^ (t << 8) ^ (t << 3) ^ (t >> 4)) & 0xFFFF
    return crc


def _frame(msgid: int, payload: bytes, crc_extra: int) -> bytes:
    hdr = bytes([len(payload), 0, 0, 0, msgid])
    return bytes([0xfe]) + hdr + payload + struct.pack('<H', mavlink_crc(hdr + payload, crc_extra))


def motor_frame(ly: float = 0.0, rx: float = 0.0, lx: float = 0.0, ry: float = 0.0, buttons: int = 0) -> bytes:
    # ly>0 = forward (robot convention is inverted -> negate)
    return _frame(202, struct.pack('<ffff', lx, -ly, rx, ry) + bytes([buttons & 0xFF]), 211)


def param_set_frame(group: str, key: str, value: float, ptype: int = 11) -> bytes:
    pid = f"{group}-{key}".encode()[:32].ljust(32, b'\x00')
    return _frame(229, struct.pack('<f', value) + bytes([255, 3]) + pid + bytes([ptype]), 208)


def command_frame(command: int) -> bytes:
    return _frame(200, struct.pack('<H', command) + bytes([255, 1]), 196)


CMD_DOCK = 40154

# Boolean feature toggles: name -> (group, key, value)
PARAM_TOGGLES = {
    "eyes_on": ("display", "enable", 1.0), "eyes_off": ("display", "enable", 0.0),
    "night_on": ("video", "night_vision", 1.0), "night_off": ("video", "night_vision", 0.0),
    "avoid_on": ("control", "auto_avoidance", 1.0), "avoid_off": ("control", "auto_avoidance", 0.0),
    "fall_on": ("control", "fallarrest", 1.0), "fall_off": ("control", "fallarrest", 0.0),
    "patrol_on": ("security_patrol", "enable", 1.0), "patrol_off": ("security_patrol", "enable", 0.0),
    "sleep": ("power", "sleep", 1.0), "wake": ("power", "sleep", 0.0),
}

# Eye animations (bonus feature). The EBO SE renders animated eyes; the upstream bridge only had on/off.
# These indices are a best-guess starting point — refine them on YOUR unit with robot/native/probe_eyes.py
# and record the results here. The brain's set_eyes() tool is generated from the keys of this map, so adding
# a confirmed animation here makes it available to the AI and the UI automatically. If none are confirmed,
# the feature still works as on/off via PARAM_TOGGLES (eyes_on / eyes_off).
EYE_ANIM_GROUP = os.environ.get("EBO_EYE_GROUP", "display")
EYE_ANIM_KEY = os.environ.get("EBO_EYE_KEY", "expression")
EYE_ANIMATIONS = {
    # name : value index (probe to confirm with probe_eyes.py; these are guesses). Kept in step with the
    # Air 2 emote catalog in robot/proto.py EYE_EMOTE_IDS so the brain's named eye states work on both paths.
    "neutral": 0.0, "happy": 1.0, "sad": 2.0, "angry": 3.0, "surprised": 4.0,
    "sleepy": 5.0, "love": 6.0, "dizzy": 7.0, "blink": 8.0, "curious": 9.0,
    "excited": 10.0, "scared": 11.0, "confused": 12.0, "wink": 13.0, "cool": 14.0,
}
