"""X86RobotLink — real-robot control on x86 / Windows via the TUTK SDK loaded with ctypes.

This is the cross-platform counterpart to `native_link.py` (which uses the bionic ARM bridge on a Pi). It
keeps the SAME safety contract (clamps live in the brain's safety floor; this link keeps its own drive
deadman) and uses `proto.py` for the protocol (byte-identical to frames.py). Only the *transport* is new.

    !!! UNTESTED !!!  The live TUTK P2P/DTLS calls below are modeled on the proven connect/recv/ioctl flow in
    `ebo-se-lan-bridge/vendor/lib/_re/sdk_x86/{tutk.py,ebo_test.py}`, but have NOT been run end-to-end yet
    (pending real keys + a real TUTK x86/Windows library). Per plan section 2 they are written to "assume it
    works when done", then promoted after the Windows hardware smoke test. The struct layout follows the
    Wyze-distributed combined lib (libIOTCAPIs_ALL); the `tutk_shim` .dll is a mock and will only validate the
    pipeline, not connect. Real control needs a real TUTK x86 build in vendor/lib (see credentials.tutk_lib_path).

Fails soft like every link: if the library or credentials are missing it logs once and reports disconnected,
so the UI + brain keep running (use mock mode for hardware-free dev).
"""
from __future__ import annotations

import ctypes
import os
import shutil
import struct
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from . import frames, proto
from ..credentials import load_credentials
from .frames import EYE_ANIMATIONS as _EYE_MAP
from .link import RobotLink

EYE_ANIMATIONS = sorted(_EYE_MAP.keys())

DRIVE_DEADMAN_S = 0.35       # stop the robot if no drive frame arrives within this window (second safety net)
AV_ER_DATA_NOREADY = -20012

# avSendIOCtrl stream-control sequence (from PROTOCOL_NOTES.md / ebo_bridge.c).
IOTYPE_STREAM_START = 0x00FF
IOTYPE_DEVICE_9930 = 0x9930
IOTYPE_STREAM_SETUP_32A = 0x032A
IOTYPE_KEEPALIVE = 0x01FF
IOTYPE_STREAM_SETUP_9936 = 0x9936
IOTYPE_AUDIO_START = 0x0300
IOTYPE_AUDIO_DATA = 0x0301


class _AVClientStartInConfig(ctypes.Structure):
    # Wyze combined-lib layout (matches sdk_x86/tutk.py). ctypes handles natural alignment.
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("iotc_session_id", ctypes.c_uint32),
        ("iotc_channel_id", ctypes.c_uint8),
        ("timeout_sec", ctypes.c_uint32),
        ("account_or_identity", ctypes.c_char_p),
        ("password_or_token", ctypes.c_char_p),
        ("resend", ctypes.c_int32),
        ("security_mode", ctypes.c_uint32),
        ("auth_type", ctypes.c_uint32),
        ("sync_recv_data", ctypes.c_int32),
    ]


class _AVClientStartOutConfig(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("server_type", ctypes.c_uint32),
        ("resend", ctypes.c_int32),
        ("two_way_streaming", ctypes.c_int32),
        ("sync_recv_data", ctypes.c_int32),
        ("security_mode", ctypes.c_uint32),
    ]


class X86RobotLink(RobotLink):
    def __init__(self):
        self.cred = load_credentials()
        self.lib_path = self.cred.tutk_lib_path()
        self._lib = None
        self._sid = -1
        self._av = -1
        self._rdt = -1
        self._running = False
        self._connected = False
        self._awake = True
        self._lock = threading.Lock()           # serialize control writes to the SDK
        self._state = {"battery": -1, "charge": 0, "frames": 0, "tx_audio": 0,
                       "toggles": {"eyes": True, "night": False, "avoid": True, "fall": True, "patrol": False},
                       "eyes_animation": "neutral"}
        self._last_frame: bytes = b""           # most recent decodable run of HEVC/H264 bytes
        self._frame_accum = bytearray()
        self._last_drive_ts = 0.0
        self._audio_sinks: list[Callable[[bytes], None]] = []
        self._threads: list[threading.Thread] = []
        self._codec = "hevc"

    # ------------- lifecycle -------------
    def start(self) -> None:
        if not self.lib_path:
            print("[x86] WARNING: no TUTK library found in vendor/lib (need a real x86/Windows TUTK build). "
                  "The robot will not connect. Use mock mode for hardware-free dev.", flush=True)
            return
        # The connect path (IOTC_Connect_ByUID + avClientStartEx with identity/token) uses these three.
        # EBO_AUTHKEY is only needed for the ByUIDEx fallback, so it's optional here (mirrors ebo_test.py).
        need = [k.upper() for k in ("uid", "identity", "token") if not getattr(self.cred, k)]
        if need:
            print(f"[x86] WARNING: missing robot credentials: {need}. Robot will not connect.", flush=True)
            return
        if not self.cred.authkey:
            print("[x86] note: EBO_AUTHKEY not set — trying IOTC_Connect_ByUID without it "
                  "(works for SE/Air in the reference flow; capture it if connect fails).", flush=True)
        try:
            self._lib = ctypes.CDLL(self.lib_path)
            self._configure_signatures()
        except Exception as e:  # noqa: BLE001
            print(f"[x86] WARNING: failed to load TUTK lib {self.lib_path}: {e}", flush=True)
            self._lib = None
            return
        self._running = True
        t = threading.Thread(target=self._connect_and_run, name="x86-conn", daemon=True)
        t.start()
        self._threads.append(t)

    def close(self) -> None:
        self._running = False
        lib = self._lib
        if lib is None:
            return
        with self._lock:
            try:
                if self._av >= 0:
                    lib.avClientStop(self._av)
                if self._sid >= 0 and hasattr(lib, "IOTC_Session_Close"):
                    lib.IOTC_Session_Close(self._sid)
            except Exception:  # noqa: BLE001
                pass

    def _configure_signatures(self):
        lib = self._lib
        lib.TUTK_SDK_Set_License_Key.argtypes = [ctypes.c_char_p]
        lib.IOTC_Initialize2.argtypes = [ctypes.c_uint16]
        lib.IOTC_Get_SessionID.restype = ctypes.c_int
        lib.IOTC_Connect_ByUID.argtypes = [ctypes.c_char_p]
        lib.IOTC_Connect_ByUID.restype = ctypes.c_int
        lib.avInitialize.argtypes = [ctypes.c_int]
        lib.avClientStartEx.argtypes = [ctypes.POINTER(_AVClientStartInConfig),
                                        ctypes.POINTER(_AVClientStartOutConfig)]
        lib.avClientStartEx.restype = ctypes.c_int
        lib.avSendIOCtrl.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_char_p, ctypes.c_int]
        lib.avSendIOCtrl.restype = ctypes.c_int
        lib.avRecvFrameData2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                         ctypes.c_char_p, ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        lib.avRecvFrameData2.restype = ctypes.c_int

    # ------------- connect + receive (UNTESTED transport) -------------
    def _connect_and_run(self):
        lib = self._lib
        try:
            lib.TUTK_SDK_Set_License_Key(self.cred.license.encode())
            lib.IOTC_Initialize2(ctypes.c_uint16(0))
            lib.avInitialize(16)
            sid = lib.IOTC_Connect_ByUID(self.cred.uid.encode("ascii"))
            if sid < 0:
                print(f"[x86] IOTC_Connect_ByUID failed rc={sid} (is the phone app closed / robot awake?)",
                      flush=True)
                return
            self._sid = sid
            # avClientStartEx: identity/token auth. Try the known security_mode/auth combos; on a remote
            # session-close (-20015) re-establish the IOTC session before the next attempt (mirrors ebo_test).
            av = -1
            for secmode, authtype in [(2, 1), (1, 1), (2, 0), (1, 0), (0, 1), (0, 0)]:
                cin = _AVClientStartInConfig()
                cin.cb = ctypes.sizeof(cin)
                cin.iotc_session_id = self._sid
                cin.iotc_channel_id = 0
                cin.timeout_sec = 20
                cin.account_or_identity = self.cred.identity.encode()
                cin.password_or_token = self.cred.token.encode()
                cin.resend = 1
                cin.security_mode = secmode
                cin.auth_type = authtype
                cout = _AVClientStartOutConfig()
                cout.cb = ctypes.sizeof(cout)
                av = lib.avClientStartEx(ctypes.byref(cin), ctypes.byref(cout))
                print(f"[x86] avClientStartEx(sec={secmode}, auth={authtype}) -> {av}", flush=True)
                if av >= 0:
                    break
                if av == -20015:
                    try:
                        if hasattr(lib, "IOTC_Session_Close"):
                            lib.IOTC_Session_Close(self._sid)
                    except Exception:  # noqa: BLE001
                        pass
                    self._sid = lib.IOTC_Connect_ByUID(self.cred.uid.encode("ascii"))
                    print(f"[x86] re-connected sid={self._sid}", flush=True)
                    if self._sid < 0:
                        break
            if av < 0:
                print(f"[x86] avClientStartEx failed rc={av} (if -20015 persists, the AV identity/token may "
                      f"be stale — re-capture; or the Air needs a different auth mode)", flush=True)
                return
            self._av = av
            self._connected = True
            self._start_stream()
            # Optional reliable-data control channel for MAVLink, if the lib exposes it.
            if hasattr(lib, "RDT_Initialize") and hasattr(lib, "RDT_Create"):
                try:
                    lib.RDT_Initialize()
                    self._rdt = lib.RDT_Create(sid, 5000, 1)
                except Exception:  # noqa: BLE001
                    self._rdt = -1
            self._spawn_workers()
            self._recv_video_loop()
        except Exception as e:  # noqa: BLE001 - fail soft, robot just won't connect
            print(f"[x86] connect error: {e}", flush=True)

    def _start_stream(self):
        """Replay the stream-start IOCtrl sequence, including the device-specific 0x9930 blob if present."""
        blob = b""
        try:
            if self.cred.ioctl9930_path and os.path.isfile(self.cred.ioctl9930_path):
                blob = open(self.cred.ioctl9930_path, "rb").read()
        except Exception:  # noqa: BLE001
            blob = b""
        z8 = b"\x00" * 8
        seq = [(IOTYPE_STREAM_START, z8[:4]), (IOTYPE_DEVICE_9930, blob or z8),
               (IOTYPE_STREAM_SETUP_32A, z8), (IOTYPE_KEEPALIVE, z8),
               (IOTYPE_STREAM_SETUP_9936, z8), (IOTYPE_AUDIO_START, z8)]
        for io, data in seq:
            self._send_ioctl(io, data)

    def _spawn_workers(self):
        for target, name in [(self._keepalive_loop, "x86-keepalive"), (self._drive_watchdog, "x86-deadman")]:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def _recv_video_loop(self):
        lib = self._lib
        buf = ctypes.create_string_buffer(800_000)
        fi = ctypes.create_string_buffer(64)
        a = ctypes.c_int(); b = ctypes.c_int(); c = ctypes.c_int(); idx = ctypes.c_int()
        while self._running:
            try:
                n = lib.avRecvFrameData2(self._av, buf, 800_000, ctypes.byref(a), ctypes.byref(b),
                                         fi, 64, ctypes.byref(c), ctypes.byref(idx))
            except Exception:  # noqa: BLE001
                break
            if n > 0:
                codec_id = fi.raw[0] if c.value > 0 else 80
                self._codec = "h264" if codec_id == 78 else "hevc"
                is_key = bool(fi.raw[1]) if c.value > 1 else False
                data = buf.raw[:n]
                if is_key:
                    self._frame_accum = bytearray(data)
                else:
                    self._frame_accum.extend(data)
                if len(self._frame_accum) > 0:
                    self._last_frame = bytes(self._frame_accum[-1_500_000:])
                self._state["frames"] += 1
            elif n == AV_ER_DATA_NOREADY:
                time.sleep(0.003)
            else:
                time.sleep(0.01)

    def _keepalive_loop(self):
        while self._running and self._connected:
            self._send_ioctl(IOTYPE_KEEPALIVE, b"\x00" * 8)
            time.sleep(10.0)

    def _drive_watchdog(self):
        """Second safety net (mirrors native link): if drive frames stop arriving, send a stop."""
        while self._running:
            if self._last_drive_ts and (time.time() - self._last_drive_ts) > DRIVE_DEADMAN_S:
                self._last_drive_ts = 0.0
                self._send_mavlink(proto.mav_motor(0.0, 0.0))
            time.sleep(0.1)

    # ------------- low-level send -------------
    def _send_ioctl(self, io_type: int, data: bytes) -> int:
        lib = self._lib
        if lib is None or self._av < 0:
            return -1
        with self._lock:
            try:
                return lib.avSendIOCtrl(self._av, ctypes.c_uint(io_type), data, len(data))
            except Exception:  # noqa: BLE001
                return -1

    def _send_mavlink(self, frame: bytes) -> int:
        """Send a MAVLink control frame over RDT if available, else wrap it as an avSendIOCtrl."""
        lib = self._lib
        if lib is None:
            return -1
        with self._lock:
            try:
                if self._rdt >= 0 and hasattr(lib, "RDT_Write"):
                    return lib.RDT_Write(self._rdt, frame, len(frame))
            except Exception:  # noqa: BLE001
                pass
        return self._send_ioctl(IOTYPE_STREAM_START, frame)

    # ------------- read -------------
    async def info(self) -> dict[str, Any]:
        s = self._state
        return {"ok": True, "connected": self._connected, "paused": False, "awake": self._awake,
                "codec": self._codec, "frames_received": s["frames"], "battery": s["battery"],
                "charge": s["charge"], "rtsp": None,
                "audio": {"codec": "0x8a", "flags": "0x00", "count": 0}}

    async def telemetry(self) -> dict[str, Any]:
        s = self._state
        return {"ok": True, "connected": self._connected, "paused": False, "awake": self._awake,
                "battery": s["battery"], "charge": s["charge"], "codec": self._codec,
                "frames_received": s["frames"], "toggles": s["toggles"],
                "eyes_animation": s["eyes_animation"], "eye_animations": EYE_ANIMATIONS,
                "audio_in": {"codec": "0x8a", "count": 0},
                "audio_out": {"sent": s["tx_audio"], "available": self._connected},
                "talk_enabled_bridge": True, "attitude": None, "imu": None,
                "rtsp": None, "variant": self.variant, "untested": True, "ts": time.time()}

    async def snapshot(self) -> tuple[bytes | None, str | None]:
        if not self._connected:
            return None, "not_connected"
        if not self._last_frame:
            return None, "no_frame_yet"
        if not shutil.which("ffmpeg"):
            return None, "ffmpeg_missing"
        try:
            p = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", self._codec if self._codec in ("h264", "hevc") else "hevc",
                 "-i", "pipe:0", "-frames:v", "1", "-f", "mjpeg", "pipe:1"],
                input=self._last_frame, capture_output=True, timeout=10,
            )
            return (p.stdout, None) if p.stdout else (None, "decode_failed")
        except Exception as e:  # noqa: BLE001
            return None, f"decode_error:{e}"

    # ------------- control -------------
    async def drive(self, ly: float, rx: float, *, generation: int | None = None,
                    epoch: int | None = None, ticket_id: int | None = None) -> dict[str, Any]:
        self._last_drive_ts = time.time()
        rc = self._send_mavlink(proto.mav_motor(ly=ly, rx=rx))
        return {"ok": rc >= 0}

    async def move(self, ly: float, rx: float, duration: float, *, generation: int | None = None,
                   epoch: int | None = None, ticket_id: int | None = None) -> dict[str, Any]:
        self._last_drive_ts = time.time()
        self._send_mavlink(proto.mav_motor(ly=ly, rx=rx))
        await _sleep(min(max(duration, 0.0), 2.0))
        self._last_drive_ts = 0.0
        self._send_mavlink(proto.mav_motor(0.0, 0.0))
        return {"ok": True}

    async def stop(self) -> dict[str, Any]:
        self._last_drive_ts = 0.0
        rc = self._send_mavlink(proto.mav_motor(0.0, 0.0))
        return {"ok": rc >= 0}

    async def action(self, name: str, *, source: str = "ai") -> dict[str, Any]:
        s = self._state
        name = name.lower()
        if name == "dock":
            self._send_mavlink(proto.mav_dock())
        elif name.startswith("eyes_") and name[5:] in EYE_ANIMATIONS:
            anim = name[5:]
            self._send_mavlink(frames.param_set_frame(frames.EYE_ANIM_GROUP, frames.EYE_ANIM_KEY,
                                                      _EYE_MAP[anim]))
            s["eyes_animation"] = anim
        else:
            # toggles: <feat>_on / <feat>_off, and wake/sleep -> power/sleep
            for tname, (grp, key, _v) in frames.PARAM_TOGGLES.items():
                if name == tname:
                    on = tname.endswith("_on") or tname == "wake"
                    self._send_mavlink(frames.param_set_frame(grp, key, 1.0 if (tname in ("sleep",)) else (1.0 if on else 0.0)))
                    break
            if name == "sleep":
                self._awake = False
            elif name == "wake":
                self._awake = True
            for feat in s["toggles"]:
                if name == f"{feat}_on":
                    s["toggles"][feat] = True
                elif name == f"{feat}_off":
                    s["toggles"][feat] = False
        return {"ok": True, "action": name}

    async def connection(self, state: str) -> dict[str, Any]:
        # x86 link holds a single session; releasing/reclaiming for the Enabot app is a future nicety.
        return {"ok": True, "paused": False}

    async def say_audio(self, g711: bytes, codec: str = "mulaw") -> dict[str, Any]:
        codec_id = 0x8A if codec == "mulaw" else 0x8B
        sent = 0
        for off in range(0, len(g711), 160):
            part = g711[off:off + 160]
            payload = bytes([codec_id, 0]) + struct.pack("<H", len(part)) + part
            if self._send_ioctl(IOTYPE_AUDIO_DATA, payload) >= 0:
                sent += 1
        self._state["tx_audio"] += sent
        return {"ok": True, "frames": sent, "available": self._connected}

    async def say_text(self, text: str) -> dict[str, Any]:
        # No local TTS on this link; the brain/server convert to G.711 and call say_audio.
        return {"ok": False, "available": False, "error": "use say_audio (server renders TTS)"}

    def set_audio_sink(self, callback) -> None:
        if callback not in self._audio_sinks:
            self._audio_sinks.append(callback)


async def _sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)
