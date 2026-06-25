"""Air2BridgeLink — let the brain (Grok) drive the cloud-controlled EBO Air 2 THROUGH the browser.

The Air 2 is Agora-based (video = RTC, control = RTM), and Agora runs in the browser. So this RobotLink
turns the brain's verbs (drive/stop/action) into `air2_cmd` events emitted over the WebSocket; the "Air 2
(cloud)" UI tab — while open and Connected — relays them to the robot over Agora RTM. The browser also POSTs
camera frames back (`set_frame`) so the brain can perceive. Net effect: Grok perceives + drives the real Air 2,
with the browser as the Agora bridge.

Requires the Air 2 (cloud) tab open + Connected. If it isn't, telemetry reports disconnected and snapshots are
unavailable (fail-soft — the robot just doesn't move, never a crash).
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional

from .link import RobotLink

EmitFn = Callable[[dict], Awaitable[None]]


class Air2BridgeLink(RobotLink):
    def __init__(self):
        self._emit: Optional[EmitFn] = None
        self._jpeg: Optional[bytes] = None
        self._jpeg_ts = 0.0
        self._browser_ts = 0.0
        self._status: dict = {}

    # --- wiring from the web server ---
    def set_emit(self, emit: EmitFn) -> None:
        self._emit = emit

    def set_frame(self, jpeg: bytes) -> None:
        """Called when the browser POSTs a fresh Agora video frame. A frame also = the bridge is alive, so it
        doubles as a liveness heartbeat (the browser streams frames ~every 1.5s while connected)."""
        if jpeg:
            self._jpeg = jpeg
            self._jpeg_ts = time.time()
            self._browser_ts = time.time()

    def set_browser(self, connected: bool, status: Optional[dict] = None) -> None:
        self._browser_ts = time.time() if connected else 0.0
        if status:
            if status.pop("drive_rejected", False):
                status["drive_rejected_ts"] = time.time()
            # Merge (don't replace) so a status-only update (e.g. battery from inbound RTM) keeps prior fields.
            self._status.update(status)
            self._process_sensors(status)

    def _process_sensors(self, status: dict) -> None:
        """Touch/bump detection from the 6-axis IMU (GrowBot-style): a sharp jump in acceleration magnitude
        between samples = something tapped/bumped/picked up the robot. Also honor an explicit touch/bump field
        if the firmware sends one. Best-effort: only fires once real IMU data is flowing in."""
        imu = status.get("imu") or status.get("accel") or {}
        ax = ay = az = None
        if isinstance(imu, dict):
            ax, ay, az = imu.get("ax", imu.get("x")), imu.get("ay", imu.get("y")), imu.get("az", imu.get("z"))
        elif isinstance(imu, (list, tuple)) and len(imu) >= 3:
            ax, ay, az = imu[0], imu[1], imu[2]
        if all(isinstance(v, (int, float)) for v in (ax, ay, az)):
            import math
            mag = math.sqrt(ax * ax + ay * ay + az * az)
            prev = self._status.get("_accel_mag")
            self._status["_accel_mag"] = mag
            if isinstance(prev, (int, float)) and abs(mag - prev) > 0.6:  # ~0.6g jolt
                self._status["touched_ts"] = time.time()
        for k in ("touch", "touched", "bump", "bumped", "collision"):
            v = status.get(k)
            if (isinstance(v, bool) and v) or (isinstance(v, (int, float)) and v):
                self._status["touched_ts"] = time.time()

    def touched(self) -> bool:
        ts = self._status.get("touched_ts", 0)
        return bool(ts) and (time.time() - ts) < 3.0

    def note_drive_rejected(self) -> None:
        """The browser saw the robot reject a drive command (RTM error 102 — happens when docked/charging).
        Treat it as a transient 'resting' signal so the brain backs off instead of fighting it."""
        self._status["drive_rejected_ts"] = time.time()

    def _browser_live(self) -> bool:
        # frames refresh _browser_ts ~every 1.5s; allow a generous gap before declaring the bridge dead.
        return self._browser_ts > 0 and (time.time() - self._browser_ts) < 20

    def _charging(self) -> int:
        # chargeStatus/adapterStatus from the Air 2 BatteryData (inbound RTM 101006), relayed by the browser.
        st = self._status
        for k in ("charge", "charging", "chargeStatus", "adapterStatus"):
            v = st.get(k)
            if isinstance(v, bool) and v:
                return 1
            if isinstance(v, (int, float)) and int(v) > 0:
                return 1
        return 0

    async def _cmd(self, **payload: Any) -> dict[str, Any]:
        if not self._emit:
            return {"ok": False, "error": "bridge not wired"}
        if not self._browser_live():
            return {"ok": False, "error": "Air 2 tab not connected — open it and press Connect"}
        await self._emit({"type": "air2_cmd", **payload})
        return {"ok": True, **payload}

    # --- read ---
    def _battery(self) -> int:
        st = self._status
        for k in ("battery", "percentage", "level", "elec", "power"):
            v = st.get(k)
            if isinstance(v, (int, float)) and 0 <= int(v) <= 100:
                return int(v)
        return -1

    async def info(self) -> dict[str, Any]:
        return {"ok": True, "connected": self._browser_live(), "awake": not self._status.get("isSleeping", False),
                "codec": "agora", "frames_received": 1 if self._jpeg else 0, "battery": self._battery(),
                "charge": self._charging(), "rtsp": None, "audio": {}}

    async def telemetry(self) -> dict[str, Any]:
        live = self._browser_live()
        sleeping = bool(self._status.get("isSleeping", False))
        st = self._status
        from ..robot.frames import EYE_ANIMATIONS
        out = {"ok": True, "connected": live, "awake": not sleeping, "battery": self._battery(),
               "charge": self._charging(), "codec": "agora", "frames_received": 1 if self._jpeg else 0,
               "toggles": {}, "eyes_animation": st.get("eyes"), "eye_animations": list(EYE_ANIMATIONS.keys()),
               "audio_in": {}, "audio_out": {"available": True, "sent": st.get("audio_sent", 0)},
               "sleeping": sleeping, "resting": self.is_resting(), "touched": self.touched(),
               "variant": "AIR2", "via": "browser_bridge", "browser_connected": live, "ts": time.time()}
        # Pass through sensor telemetry when the robot reports it (6-axis IMU + IR time-of-flight distance).
        for k in ("imu", "accel", "gyro", "tof", "distance", "obstacle", "wifi", "wifiStrength"):
            if k in st:
                out[k] = st[k]
        return out

    def is_resting(self) -> bool:
        """Robot is unavailable for driving: charging, asleep, or it just rejected a drive (docked)."""
        if self._charging() or self._status.get("isSleeping"):
            return True
        rej = self._status.get("drive_rejected_ts", 0)
        return bool(rej) and (time.time() - rej) < 30

    async def snapshot(self) -> tuple[bytes | None, str | None]:
        if self._jpeg and (time.time() - self._jpeg_ts) < 15:
            return self._jpeg, None
        if not self._browser_live():
            return None, "Air 2 tab not connected (open it + Connect so the brain can see)"
        return None, "no_frame_yet"

    # --- control (relayed to the browser, which sends Agora RTM) ---
    async def drive(self, ly: float, rx: float, *, generation: int | None = None,
                    epoch: int | None = None, ticket_id: int | None = None) -> dict[str, Any]:
        return await self._cmd(cmd="drive", ly=ly, rx=rx, duration=0.0)

    async def move(self, ly: float, rx: float, duration: float, *, generation: int | None = None,
                   epoch: int | None = None, ticket_id: int | None = None) -> dict[str, Any]:
        return await self._cmd(cmd="drive", ly=ly, rx=rx, duration=duration)

    async def stop(self) -> dict[str, Any]:
        return await self._cmd(cmd="stop")

    async def action(self, name: str, *, source: str = "ai") -> dict[str, Any]:
        # Eye/expression actions (eyes_happy, eyes_blink, ...) -> a dedicated 'eyes' command the browser maps
        # to the Air 2 emote channel (RTM_EMOTE). Everything else passes through as a generic action.
        n = (name or "").lower()
        if n.startswith("eyes_") and n not in ("eyes_on", "eyes_off"):
            state = n[len("eyes_"):]
            self._status["eyes"] = state
            return await self._cmd(cmd="eyes", state=state)
        return await self._cmd(cmd="action", name=name)

    async def connection(self, state: str) -> dict[str, Any]:
        return {"ok": True}

    async def say_audio(self, g711: bytes, codec: str = "mulaw") -> dict[str, Any]:
        return {"ok": False, "available": False, "error": "talkback not wired for Air 2 yet"}

    def prefers_text_tts(self) -> bool:
        return True

    async def say_text(self, text: str) -> dict[str, Any]:
        # relayed to the browser, which fetches server TTS (WAV) and publishes it into the Agora call
        return await self._cmd(cmd="say", text=text)
