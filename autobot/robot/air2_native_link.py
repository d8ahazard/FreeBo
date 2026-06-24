"""Air2NativeLink — the FULLY NATIVE EBO Air 2 link: no browser, no WSL.

Two halves, both server-side, sharing one cloud session (exactly like the app does):
  * CONTROL  — `RtmNode` runs the real Agora RTM SDK headless in Node and sends the eboproto drive/eyes/dock
               messages, and ingests inbound robot status (battery/charge/IMU/TOF/touch).
  * MEDIA    — `AgoraNativeReceiver` joins the Agora RTC channel in pure Python (aiortc + our H265 depacketizer)
               and decodes the robot's video into a `MediaHub`; `snapshot()` serves the latest frame.

Why both: the robot only acts on drive commands while an app participant is actually in the RTC channel (an
active "call"). RTM alone connects but the robot stays put — so we bring up RTC too, just like the phone app.

Session sharing: one cached session (refreshed ~every 3 min, re-minted on token expiry) feeds both the RTM
sidecar (rtm uid/token) and the RTC receiver (rtc uid/token) — one RTM + one RTC participant, same as the app.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Optional

from .link import RobotLink
from .media_hub import MediaHub

# named eye states -> emote (mirror proto.py / sidecar)
_EYE_STATES = {"neutral", "happy", "sad", "angry", "surprised", "sleepy", "love", "dizzy",
               "blink", "curious", "excited", "scared", "confused", "wink", "cool"}


class Air2NativeLink(RobotLink):
    variant = "AIR2"

    def __init__(self) -> None:
        from .agora_native import AgoraNativeReceiver, build_rtp_capabilities
        from .rtm_node import RtmNode
        self.hub = MediaHub()
        self._sess: Optional[dict] = None
        self._sess_ts = 0.0
        self._sess_lock = threading.Lock()
        self._robot_id = int(os.environ.get("EBO_ROBOT_ID", "0") or 0)
        self.rtm = RtmNode(self._provider, on_event=self._on_rtm_event)
        # Audio: ALWAYS declare RECV audio in the RTC join so the gateway forwards the robot's mic (needed for
        # STT/voice — sniffing proved the RTM mic handshake was already correct; the missing piece was the
        # recv-audio capability). SEND audio (talkback onto the robot's speaker) is on by default; disable with
        # AUTOBOT_AIR2_NATIVE_TALK=0. Either way we now pass real caps (never None), so mic always flows.
        self._native_talk = os.environ.get("AUTOBOT_AIR2_NATIVE_TALK", "1").strip().lower() in (
            "1", "true", "yes", "on")
        caps = build_rtp_capabilities(send_audio=self._native_talk)
        self.receiver = AgoraNativeReceiver(session_provider=self._provider, hub=self.hub,
                                            rtp_capabilities=caps)
        self._media_started = False
        self._last_drive_rejected = 0.0
        self._batt_ts = 0.0
        self._audio_req_ts = 0.0
        self._tx_audio = 0
        self._call_open = False   # have we run the 102001->102003 call-mode handshake this session?
        self._last_audio_seen = 0   # last observed hub audio_count — re-open the mic when it stops growing
        self._paused = False      # go-dark: stop all robot I/O (drive/audio/media) but keep the session warm

    # --- shared session provider (used by BOTH the RTM sidecar and the RTC receiver) ---
    async def _provider(self, force: bool = False) -> dict:
        """Mint/return a cloud session. `force` bypasses the cache — used when a token expired and we must
        re-login with fresh credentials (the cached one is dead)."""
        if not force:
            with self._sess_lock:
                if self._sess and self._sess.get("ok") and (time.time() - self._sess_ts) < 120:
                    return self._sess
        from .ebo_cloud import EboCloud
        sess = await EboCloud().create_session(self._robot_id)
        if isinstance(sess, dict) and sess.get("ok"):
            with self._sess_lock:
                self._sess = sess
                self._sess_ts = time.time()
        return sess

    def _on_rtm_event(self, ev: dict) -> None:
        if ev.get("ev") == "peer":
            p = ev.get("parsed") or {}
            if p.get("drive_rejected"):
                self._last_drive_rejected = time.time()

    @staticmethod
    def _find_battery(obj) -> Optional[int]:
        """Deep-search a cloud JSON response for a battery-like 0..100 number (field shape varies by region)."""
        keys = ("electric", "battery", "power", "percent", "soc", "elec", "capacity", "quantity")
        found: list[Optional[int]] = [None]

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, (int, float)) and 0 <= v <= 100 and any(kk in str(k).lower() for kk in keys):
                        found[0] = int(v)
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(obj)
        return found[0]

    async def _maybe_poll_battery(self) -> None:
        """Throttled cloud poll for battery/charge — the robot's inbound RTM telemetry is unreliable, but the
        signed cloud REST knows the device's charge state."""
        if time.time() - self._batt_ts < 30:
            return
        self._batt_ts = time.time()
        try:
            from .ebo_cloud import EboCloud
            status, body = await EboCloud().request("GET", "/api/v1/ebox/robots/robot")
            if status == 200:
                b = self._find_battery(body)
                if b is not None:
                    self.rtm.status["battery"] = b
        except Exception:  # noqa: BLE001
            pass

    # --- lifecycle ---
    def start(self) -> None:
        # RTM control comes up immediately (its own thread). Media (RTC) needs the asyncio loop, so it's
        # started lazily from the first async call (telemetry/snapshot), which runs on the server loop.
        self.rtm.start()

    def _ensure_media(self) -> None:
        if self._media_started:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._media_started = True
        self.receiver.start()

    def close(self) -> None:
        try:
            self.rtm.stop()
        except Exception:  # noqa: BLE001
            pass

    # --- read ---
    def _connected(self) -> bool:
        # Live if RTM control is up OR video is actively flowing — the RTM SDK briefly reports non-CONNECTED
        # during keepalive/token state-changes even though control never drops, so don't blink OFFLINE on it.
        if self.rtm.connected:
            return True
        return (time.monotonic() - (self.hub.last_video_ts or 0)) < 5.0

    def _tel(self) -> dict:
        """Merged robot telemetry: RTM peer messages + the RTC gateway-WS telemetry the receiver parses."""
        merged = dict(getattr(self.receiver, "telemetry", {}) or {})
        merged.update(self.rtm.status or {})   # RTM peer values win if both present
        return merged

    def _battery(self) -> int:
        v = self._tel().get("battery")
        return int(v) if isinstance(v, (int, float)) and 0 <= int(v) <= 100 else -1

    def _charging(self) -> int:
        return 1 if int(self._tel().get("charge", 0) or 0) > 0 else 0

    def is_resting(self) -> bool:
        if self._charging():
            return True
        # Critically low battery: the robot physically can't drive (no power for motors) even though it still
        # streams video + accepts commands. Treat as resting so the brain stops futilely commanding a dead
        # robot and surfaces a clear LOW-BATTERY state instead.
        batt = self._battery()
        if 0 <= batt <= 8:
            return True
        return bool(self._last_drive_rejected) and (time.time() - self._last_drive_rejected) < 30

    def _touched(self) -> bool:
        st = self._tel()
        for k in ("touch", "touched", "bump", "bumped", "collision"):
            v = st.get(k)
            if (isinstance(v, bool) and v) or (isinstance(v, (int, float)) and v):
                return True
        return False

    async def info(self) -> dict[str, Any]:
        self._ensure_media()
        jpeg = self.hub.latest_video_jpeg()
        return {"ok": True, "connected": self._connected(), "awake": True, "codec": "agora",
                "frames_received": 1 if jpeg else 0, "battery": self._battery(), "charge": self._charging()}

    async def _maybe_enable_audio(self) -> None:
        """Tell the robot to stream its mic. Reverse-engineered from the EBO app: opening the live view sends
        the FULL two-way audio handshake (RTM 102001 {open:1,type:1} 'audio session on' THEN 102003
        {open:1,type:1} 'intercom on'). 102001 ALONE is not enough — the robot only streams its mic reliably
        once 102003 is also sent (verified live: with 102001 only, mic audio is ~zero; after 102003 it flows).
        Re-sent periodically (102003 keepalive) so it survives reconnects. This is independent of talkback —
        we need the robot's mic for STT/voice commands even when we're not publishing audio."""
        if self._paused or not self._connected():
            return
        # Poll every 8s. The robot drops its mic stream after a while (and RTM sendMessageToPeer is flaky, so a
        # one-shot 102001 can silently fail), so we detect liveness by whether NEW audio packets arrived since
        # last check (recency — NOT cumulative count, which never resets). If audio stalled, re-fire the FULL
        # 102001->102003 handshake to re-open the mic; while it's flowing, just keepalive 102003 (re-handshaking
        # mid-stream clicks/cuts). Drive survives the same RTM flakiness because it's spammed at 10 Hz.
        # Re-open fast when the mic has stalled (the robot drops it between handshakes, especially with outbound
        # talk off), back off once it's flowing so we don't thrash a healthy stream.
        cnt = self.hub.stats().get("audio_count", 0)
        audio_live = cnt > self._last_audio_seen
        interval = 12 if audio_live else 3
        if time.time() - self._audio_req_ts < interval:
            return
        self._audio_req_ts = time.time()
        self._last_audio_seen = cnt
        try:
            await self._open_call_mode(force=not audio_live)
            # The robot only SUSTAINS its mic during an active TWO-WAY call: it needs to see continuous
            # outbound audio from us (like the app does in live view), otherwise it sends one short burst after
            # the handshake and stops. Start our G.711 publish loop (silence keepalive + any queued TTS) so the
            # call stays open and the robot keeps streaming its mic. Idempotent (no-op once running).
            if self._native_talk and hasattr(self.receiver, "ensure_publishing"):
                self.receiver.ensure_publishing()
        except Exception:  # noqa: BLE001
            pass

    async def telemetry(self) -> dict[str, Any]:
        self._ensure_media()
        await self._maybe_enable_audio()
        await self._maybe_poll_battery()
        from .frames import EYE_ANIMATIONS
        st = self._tel()
        jpeg = self.hub.latest_video_jpeg()
        out = {"ok": True, "connected": self._connected(), "awake": True, "battery": self._battery(),
               "charge": self._charging(), "codec": "agora", "frames_received": 1 if jpeg else 0,
               "toggles": {}, "eyes_animation": st.get("eyes"), "eye_animations": list(EYE_ANIMATIONS.keys()),
               "audio_in": {}, "audio_out": {"available": True, "sent": self._tx_audio},
               "sleeping": False, "paused": self._paused, "resting": self.is_resting(), "touched": self._touched(),
               "variant": "AIR2", "via": "native_rtm+rtc", "browser_connected": False,
               "video_frames": self.hub.stats().get("video_count", 0),
               "audio_frames": self.hub.stats().get("audio_count", 0), "ts": time.time()}
        for k in ("imu", "accel", "gyro", "tof", "distance", "obstacle", "wifi", "wifiStrength",
                  "laser", "moveSpeed", "moveMode", "lowBatteryPercentage", "liveStatus", "avoidobstacle"):
            if k in st:
                out[k] = st[k]
        return out

    async def snapshot(self) -> tuple[bytes | None, str | None]:
        self._ensure_media()
        jpeg = self.hub.latest_video_jpeg(max_w=960, quality=75)
        if jpeg:
            return jpeg, None
        if not self._connected():
            return None, "RTM not connected (native link starting up)"
        return None, "no_frame_yet"

    # --- control (native RTM) ---
    async def drive(self, ly: float, rx: float) -> dict[str, Any]:
        ok = self.rtm.drive(ly, rx, 0.0)
        return {"ok": ok, "ly": ly, "rx": rx}

    async def move(self, ly: float, rx: float, duration: float) -> dict[str, Any]:
        ok = self.rtm.drive(ly, rx, duration)
        return {"ok": ok, "ly": ly, "rx": rx, "duration": duration}

    async def stop(self) -> dict[str, Any]:
        return {"ok": self.rtm.stop()}

    async def action(self, name: str) -> dict[str, Any]:
        n = (name or "").lower()
        if n.startswith("eyes_"):
            state = n[len("eyes_"):]
            if state in _EYE_STATES:
                self.rtm.status["eyes"] = state
                return {"ok": self.rtm.eyes(state), "eyes": state}
            return {"ok": False, "error": f"unknown eye state {state}"}
        if n == "dock":
            return {"ok": self.rtm.dock(), "docking": True}
        if n.startswith("avoid"):
            return {"ok": self.rtm.avoid(n != "avoid_off")}
        if n.startswith("laser"):
            on = n not in ("laser_off", "laser_0")
            return {"ok": self.rtm.raw(103051, {"laser": on}), "laser": on}
        if n in ("release", "release_control"):
            return {"ok": self.rtm._send({"cmd": "release"}), "released": True}
        if n in ("resume", "resume_control"):
            return {"ok": self.rtm._send({"cmd": "resume"}), "resumed": True}
        if n in ("dock_release", "go_home"):
            # dock + release our controller heartbeat so the robot's onboard return-to-charge can run
            return {"ok": self.rtm._send({"cmd": "dock_release"}), "docking": True, "released": True}
        return {"ok": False, "error": f"action '{name}' not supported on native Air2"}

    async def connection(self, state: str) -> dict[str, Any]:
        """Go-dark / wake. `stop` cuts all robot I/O (stop drive, release controller, drop inbound media) but
        keeps the Agora RTC/RTM session warm so `start` resumes instantly. `start` re-acquires control + mic."""
        if state == "stop":
            self._paused = True
            for fn in (lambda: self.rtm.stop(),
                       lambda: self.rtm._send({"cmd": "release"})):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.receiver.set_paused(True)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "paused": True}
        # start / resume
        self._paused = False
        try:
            self.receiver.set_paused(False)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.rtm._send({"cmd": "resume"})
        except Exception:  # noqa: BLE001
            pass
        self._audio_req_ts = 0.0   # force mic re-enable (102001) on the next telemetry tick
        self._call_open = False
        return {"ok": True, "paused": False}

    def prefers_text_tts(self) -> bool:
        return True

    async def _open_call_mode(self, force: bool = False) -> None:
        """Replay the EBO app's exact CALL-MODE enable handshake so the robot actually opens its two-way audio
        path (and routes published RTP to its speaker). Sniffed from the phone:
            102001 {open:1,type:1}   # open the audio session (robot starts/accepts audio)
            ~1.5s later
            102003 {open:1,type:1}   # intercom direction app->robot ON
        Sending 102003 alone does nothing — the call session was never opened. `force` re-sends the FULL
        sequence (used by the retry loop while the mic hasn't started); otherwise once open we just keepalive
        102003 (re-handshaking while audio flows clicks/cuts)."""
        if self._call_open and not force:
            self.rtm.raw(102003, {"open": 1, "type": 1})   # keepalive — cheap, survives robot-side timeouts
            return
        self.rtm.raw(102001, {"open": 1, "type": 1})       # open audio/call session
        await asyncio.sleep(1.2)                            # let the robot bring up its audio pipeline
        self.rtm.raw(102003, {"open": 1, "type": 1})       # intercom app->robot ON
        await asyncio.sleep(0.3)
        self._call_open = True
        self._audio_req_ts = time.time()                   # we just sent 102001; don't double-send right away

    async def publish_speech(self, wav_bytes: bytes) -> dict[str, Any]:
        """Play a rendered TTS clip (WAV bytes) on the robot's own speaker via the RTC audio publish path.

        TTS is rendered by the caller (brain/web) so this stays out of the robot layer. We open CALL MODE the
        way the phone does (102001 then 102003) so the robot accepts our audio, then stream the clip as G.711
        A-law into the RTC channel. Fail-soft."""
        if not wav_bytes:
            return {"ok": False, "available": True, "error": "no audio (TTS produced nothing)"}
        self._ensure_media()
        if not self.receiver.can_publish():
            return {"ok": False, "available": True, "error": "RTC channel not ready for audio publish yet"}
        try:
            await self._open_call_mode()
        except Exception:  # noqa: BLE001
            pass
        res = await self.receiver.publish_audio(wav_bytes)
        if res.get("ok"):
            self._tx_audio += 1
        res.setdefault("available", True)
        return res

    async def say_text(self, text: str) -> dict[str, Any]:
        # The brain/web render WAV and call publish_speech() directly; this is a fallback for raw-text callers.
        return {"ok": False, "available": True, "error": "use publish_speech(wav) for Air 2 talkback"}

    async def say_audio(self, g711: bytes, codec: str = "mulaw") -> dict[str, Any]:
        return {"ok": False, "available": True, "error": "use publish_speech(wav) for Air 2 talkback"}
