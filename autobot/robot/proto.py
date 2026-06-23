"""Protocol facade over the `eboproto` codec — the single place the app turns logical robot actions into
wire bytes, and decides which transport channel they belong on (LAN MAVLink vs cloud RTM JSON).

Two backends, picked automatically and fail-soft:
  1. **ctypes `libeboproto`** — the vendored C codec (autobot/robot/native/eboproto), if a shared lib has
     been built (e.g. by scripts/bootstrap.py `make shared`). Preferred when present.
  2. **pure Python** — `frames.py` for MAVLink (already byte-identical to eboproto, proven by the self-test)
     + small JSON builders for the RTM plane. The default everywhere, so clone-and-run needs no compiler.

This module is transport-agnostic, exactly like eboproto: it produces bytes, it does NOT open sessions /
sockets. `route()` reports which channel an action uses for a given Enabot model so a link can dispatch it.

See docs/BRIDGE_PROTOCOL.md, autobot/robot/native/eboproto/README.md, and the RTM map in
ebo-se-lan-bridge/vendor/lib/_re/PROTOCOL_NOTES.md.
"""
from __future__ import annotations

import ctypes
import json
import os
import time
from pathlib import Path
from typing import Optional

from . import frames

# ---- transport channels (mirror eboproto's ebo_channel_t) ----
CH_NONE = "none"
CH_RDT_MAVLINK = "rdt_mavlink"   # full MAVLink frame -> RDT reliable channel (bridge kind 0)
CH_AV_IOCTL = "av_ioctl"         # avSendIOCtrl payload -> bridge kind 1
CH_AV_AUDIO = "av_audio"         # outbound audio -> bridge kind 2
CH_RTM_JSON = "rtm_json"         # UTF-8 JSON command -> cloud RTM socket

# ---- RTM command ids (recovered from a live EBO Air 2; see PROTOCOL_NOTES.md) ----
RTM_DRIVE = 101007
RTM_EMOTE = 103003
RTM_MOVE_MODE = 103011
RTM_DOCK = 103043
RTM_AVOID = 103045
RTM_LASER = 103051
RTM_SHOOT_MODE = 102035

VARIANTS = ("GENERIC", "SE", "AIR", "AIR2", "PRO")

# Named eye/emotion states -> Air 2 emote emojiId (RTM_EMOTE 103003). The robot renders preset eye
# expressions (no per-pixel bitmap exists in the protocol). These ids are a best-guess catalog to be
# confirmed by reverse-engineering the app's emote list (see plan: eyes-enumerate); the browser also maps
# them, so refining here + there keeps the named states working. The brain only ever uses the NAMES.
EYE_EMOTE_IDS = {
    "neutral": 0, "happy": 1, "sad": 2, "angry": 3, "surprised": 4,
    "sleepy": 5, "love": 6, "dizzy": 7, "blink": 8, "curious": 9,
    "excited": 10, "scared": 11, "confused": 12, "wink": 13, "cool": 14,
}


def eye_emote_id(state: str) -> int:
    return EYE_EMOTE_IDS.get((state or "").lower(), 0)

# Actions that AIR2/PRO route over the cloud RTM plane; everything else (night/patrol/fall/eyes/sleep) stays
# on the LAN MAVLink param path even on those models (verified in the capture — see PROTOCOL_NOTES.md).
_RTM_MOTION_ACTIONS = {"drive", "stop", "dock", "avoid", "laser", "shoot_mode", "move_mode", "emote", "eyes_anim"}


# ====================================================================
# Optional ctypes backend
# ====================================================================
def _find_lib() -> Optional[str]:
    names = ["libeboproto.so", "libeboproto.dll", "eboproto.dll", "libeboproto.dylib"]
    here = Path(__file__).resolve().parent
    roots = [
        here / "native" / "eboproto",            # built in place by `make shared`
        Path(os.environ.get("EBOPROTO_LIB", "")) if os.environ.get("EBOPROTO_LIB") else here,
        here.parents[1] / "data",                # data/ runtime dir
    ]
    for r in roots:
        for n in names:
            p = r / n
            if p.is_file():
                return str(p)
    # explicit full path override
    explicit = os.environ.get("EBOPROTO_LIB")
    if explicit and os.path.isfile(explicit):
        return explicit
    return None


def _load_lib():
    path = _find_lib()
    if not path:
        return None
    try:
        lib = ctypes.CDLL(path)
        # Minimal signatures for the builders we delegate. Buffers are caller-provided (eboproto never allocs).
        lib.ebo_mav_motor.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                                      ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_uint8]
        lib.ebo_mav_motor.restype = ctypes.c_int
        return lib
    except Exception:  # noqa: BLE001 - any ABI/load problem -> fall back to Python
        return None


_LIB = _load_lib()


def using_native() -> bool:
    """True if the ctypes libeboproto backend is active (else the pure-Python frames.py path is used)."""
    return _LIB is not None


# ====================================================================
# MAVLink builders (LAN / RDT plane). frames.py is the byte-identical Python source of truth; if the C lib
# is loaded we use it (and silently fall back to frames.py on any error).
# ====================================================================
def mav_motor(ly: float = 0.0, rx: float = 0.0, lx: float = 0.0, ry: float = 0.0, buttons: int = 0) -> bytes:
    if _LIB is not None:
        try:
            buf = ctypes.create_string_buffer(frames_MAX)
            n = _LIB.ebo_mav_motor(buf, frames_MAX, ctypes.c_float(lx), ctypes.c_float(ly),
                                   ctypes.c_float(rx), ctypes.c_float(ry), ctypes.c_uint8(buttons & 0xFF))
            if n > 0:
                return buf.raw[:n]
        except Exception:  # noqa: BLE001
            pass
    return frames.motor_frame(ly=ly, rx=rx, lx=lx, ry=ry, buttons=buttons)


def mav_param_set(group: str, key: str, value: float) -> bytes:
    return frames.param_set_frame(group, key, value)


def mav_command(command: int) -> bytes:
    return frames.command_frame(command)


def mav_dock() -> bytes:
    return frames.command_frame(frames.CMD_DOCK)


frames_MAX = frames.__dict__.get("EBO_MAV_MAX_FRAME", 48)


# ====================================================================
# Cloud RTM JSON builders (Air 2 / EBO Max plane). Shape recovered from live capture:
#   {"id":<int>,"sid":"<session>","data":{...},"type":0,"timestamp":<ms>}
# ====================================================================
def _rtm(msg_id: int, sid: Optional[str], data: Optional[dict]) -> str:
    return json.dumps({
        "id": msg_id,
        "sid": sid or "",
        "data": data or {},
        "type": 0,
        "timestamp": int(time.time() * 1000),
    }, separators=(",", ":"))


def _clamp100(v: float) -> int:
    return max(-100, min(100, int(round(v))))


def rtm_drive(sid: Optional[str], lx: float = 0.0, ly: float = 0.0, rx: float = 0.0, ry: float = 0.0,
              buttons: int = 0) -> str:
    return _rtm(RTM_DRIVE, sid, {"lx": _clamp100(lx), "ly": _clamp100(ly), "rx": _clamp100(rx),
                                 "ry": _clamp100(ry), "buttons": int(buttons) & 0xFF})


def rtm_dock(sid: Optional[str]) -> str:
    return _rtm(RTM_DOCK, sid, None)


def rtm_avoid(sid: Optional[str], on: bool) -> str:
    return _rtm(RTM_AVOID, sid, {"avoidobstacle": bool(on)})


def rtm_laser(sid: Optional[str], on: bool) -> str:
    return _rtm(RTM_LASER, sid, {"laser": bool(on)})


def rtm_shoot_mode(sid: Optional[str], mode: int) -> str:
    return _rtm(RTM_SHOOT_MODE, sid, {"shootMode": int(mode)})


def rtm_move_mode(sid: Optional[str], mode: int) -> str:
    return _rtm(RTM_MOVE_MODE, sid, {"moveMode": int(mode)})


def rtm_emote(sid: Optional[str], emoji: Optional[list] = None, voice: Optional[list] = None,
              move: Optional[list] = None, cycle_mode: int = 0) -> str:
    return _rtm(RTM_EMOTE, sid, {"voiceIds": list(voice or []), "cycleMode": int(cycle_mode),
                                 "emojiIds": list(emoji or []), "moveIds": list(move or [])})


# ====================================================================
# Per-variant routing — which channel an action uses for a given model.
# ====================================================================
def normalize_variant(variant: str) -> str:
    v = (variant or "SE").upper()
    return v if v in VARIANTS else "SE"


def route(variant: str, action: str) -> str:
    """Return the transport channel for (variant, action). SE/AIR/GENERIC are LAN MAVLink; AIR2/PRO use the
    cloud RTM plane for motion-ish actions and LAN MAVLink param for night/patrol/fall/eyes/sleep."""
    v = normalize_variant(variant)
    a = action.lower()
    if v in ("AIR2", "PRO") and a in _RTM_MOTION_ACTIONS:
        return CH_RTM_JSON
    return CH_RDT_MAVLINK
