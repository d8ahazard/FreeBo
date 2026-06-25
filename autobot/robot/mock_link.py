"""A fake robot link for hardware-free development.

Mimics the real link (telemetry, a synthetic camera snapshot, and all control verbs) so you can exercise
the whole brain + UI + safety floor with no robot. Control calls are logged and reflected in telemetry
(toggles, eyes, drive state). Select it with AUTOBOT_ROBOT_LINK=mock. See docs/AI_BRAIN.md.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

from .frames import EYE_ANIMATIONS as _EYE_MAP
from .link import RobotLink

EYE_ANIMATIONS = sorted(_EYE_MAP.keys())


def _solid_jpeg() -> bytes:
    # Minimal valid 1x1 grey JPEG (so the endpoint always returns something decodable).
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c19"
        "1213100e1416141a1c1d1d1318202329211e242a1f1d1e25272a262e29242d2c2e30332e272f3131303339393b3c3a3a"
        "32373c2c3a3a3affc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405"
        "060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328"
        "1a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a53545556"
        "5758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3"
        "b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda00"
        "08010100003f00bf800a28a28affd9"
    )


class MockRobotLink(RobotLink):
    def __init__(self):
        self.state: dict[str, Any] = {
            "battery": 87, "charge": 0, "awake": True, "connected": True, "paused": False,
            "toggles": {"eyes": True, "night": False, "avoid": True, "fall": True, "patrol": False},
            "eyes_animation": "neutral", "last_drive": (0.0, 0.0), "tx_audio": 0, "frames": 1000,
        }
        self._snap_cache: tuple[bytes, float] = (b"", 0.0)
        self._seq = 0
        self._freeze_seq = False   # test knob: when True, snapshot_sample reuses the last seq (stale stream)

    # --- vision ---
    def _make_jpeg(self) -> bytes:
        """A synthetic camera frame. Prefer ffmpeg's testsrc2 (varies over time); fall back to a tiny
        solid JPEG if ffmpeg is unavailable."""
        if shutil.which("ffmpeg"):
            t = int(time.time()) % 30
            try:
                p = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error",
                     "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=1,format=yuv420p",
                     "-ss", str(t), "-frames:v", "1", "-f", "mjpeg", "pipe:1"],
                    check=True, capture_output=True, timeout=10,
                )
                if p.stdout:
                    return p.stdout
            except Exception:  # noqa: BLE001
                pass
        return _solid_jpeg()

    def _snapshot_bytes(self) -> bytes:
        jpeg, ts = self._snap_cache
        now = time.time()
        if now - ts > 1.5 or not jpeg:
            jpeg = self._make_jpeg()
            self._snap_cache = (jpeg, now)
        return jpeg

    # --- read ---
    async def info(self) -> dict[str, Any]:
        s = self.state
        return {"ok": True, "connected": s["connected"], "paused": s["paused"], "awake": s["awake"],
                "codec": "hevc", "frames_received": s["frames"], "battery": s["battery"],
                "charge": s["charge"], "rtsp": "rtsp://127.0.0.1:8554/ebo",
                "audio": {"codec": "0x8a", "flags": "0x09", "count": 10}}

    async def telemetry(self) -> dict[str, Any]:
        s = self.state
        return {"ok": True, "connected": s["connected"], "paused": s["paused"], "awake": s["awake"],
                "battery": s["battery"], "charge": s["charge"], "codec": "hevc",
                "frames_received": s["frames"], "toggles": s["toggles"],
                "eyes_animation": s["eyes_animation"], "eye_animations": EYE_ANIMATIONS,
                "audio_in": {"codec": "0x8a", "count": 10},
                "audio_out": {"sent": s["tx_audio"], "available": True},
                "talk_enabled_bridge": True, "attitude": None, "imu": None,
                "rtsp": "rtsp://127.0.0.1:8554/ebo", "ts": time.time()}

    async def snapshot(self) -> tuple[bytes | None, str | None]:
        if not self.state["awake"]:
            return None, "asleep_or_not_ready"
        return self._snapshot_bytes(), None

    async def snapshot_sample(self):
        """Sequence-aware snapshot with a controllable monotonic seq, so tests can drive fresh vs. stale-frame
        motion evidence. Set `_freeze_seq=True` to simulate a stalled stream (same seq -> evidence UNKNOWN)."""
        import time as _t

        from .media_hub import FrameSample
        if not self.state["awake"]:
            return FrameSample(jpeg=None, seq=None, wall_ts=_t.monotonic(), age=0.0, valid=False,
                               error="asleep_or_not_ready")
        if not self._freeze_seq:
            self._seq += 1
        return FrameSample(jpeg=self._snapshot_bytes(), seq=self._seq, wall_ts=_t.monotonic(),
                           age=0.0, valid=True)

    # --- control ---
    async def drive(self, ly: float, rx: float, *, generation: int | None = None,
                    epoch: int | None = None) -> dict[str, Any]:
        self.state["last_drive"] = (ly, rx)
        self.state["last_ticket"] = {"generation": generation, "epoch": epoch}
        print(f"[mock] drive ly={ly} rx={rx} gen={generation} epoch={epoch}", flush=True)
        return {"ok": True}

    async def move(self, ly: float, rx: float, duration: float, *, generation: int | None = None,
                   epoch: int | None = None) -> dict[str, Any]:
        self.state["last_drive"] = (ly, rx)
        self.state["last_ticket"] = {"generation": generation, "epoch": epoch}
        print(f"[mock] move ly={ly} rx={rx} dur={duration} gen={generation} epoch={epoch}", flush=True)
        return {"ok": True}

    async def stop(self) -> dict[str, Any]:
        self.state["last_drive"] = (0.0, 0.0)
        print("[mock] stop", flush=True)
        return {"ok": True}

    async def action(self, name: str, *, source: str = "ai") -> dict[str, Any]:
        print(f"[mock] action {name}", flush=True)
        s = self.state
        if name.startswith("eyes_") and name[5:] in EYE_ANIMATIONS:
            s["eyes_animation"] = name[5:]; s["toggles"]["eyes"] = True
        for feat in s["toggles"]:
            if name == f"{feat}_on":
                s["toggles"][feat] = True
            elif name == f"{feat}_off":
                s["toggles"][feat] = False
        if name == "sleep":
            s["awake"] = False
        if name == "wake":
            s["awake"] = True
        return {"ok": True, "action": name}

    async def connection(self, state: str) -> dict[str, Any]:
        self.state["paused"] = (state == "stop")
        print(f"[mock] connection {state}", flush=True)
        return {"ok": True, "paused": self.state["paused"]}

    async def say_audio(self, g711: bytes, codec: str = "mulaw") -> dict[str, Any]:
        self.state["tx_audio"] += 1
        print(f"[mock] say_audio bytes={len(g711)} codec={codec}", flush=True)
        return {"ok": True, "frames": max(1, len(g711) // 160), "available": True}

    async def say_text(self, text: str) -> dict[str, Any]:
        self.state["tx_audio"] += 1
        print(f"[mock] say_text text={text!r}", flush=True)
        return {"ok": True, "frames": 1, "available": True}
