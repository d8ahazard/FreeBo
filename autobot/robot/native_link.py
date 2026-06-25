"""NativeRobotLink — drives the real EBO SE through the native TUTK bridge.

This is the in-process successor to the old Pi `supervisor.py`. It owns the only session to the robot:
it spawns mediamtx (RTSP/WebRTC/HLS), ffmpeg (H.265 passthrough), and the native `ebo_bridge` binary (run
under the bundled bionic linker), forwards video to ffmpeg, parses inbound status (battery), keeps a JPEG
snapshot tap for the AI's vision, and sends control/talkback frames. It exposes the async RobotLink verbs.

Robot secrets come from `autobot.credentials` and are passed to the native child via env only; they are
never logged here. The bridge-side deadman watchdog (stop the robot if /drive frames stop arriving) lives
here as the second safety layer — keep it. See docs/BRIDGE_PROTOCOL.md and docs/SAFETY.md.
"""
from __future__ import annotations

import asyncio
import audioop
import os
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..credentials import load_credentials
from . import video
from .frames import (
    CMD_DOCK,
    EYE_ANIM_GROUP,
    EYE_ANIM_KEY,
    EYE_ANIMATIONS,
    PARAM_TOGGLES,
    command_frame,
    motor_frame,
    param_set_frame,
)
from .link import RobotLink

# Pi-only knobs (not UI-editable). Robot audio listen + talkback behavior.
AUDIO_ON = os.environ.get("EBO_AUDIO", "0") == "1"
AUDIO_FMT = os.environ.get("EBO_AUDIO_FMT", "mulaw")   # mulaw | alaw
AUDIO_RATE = os.environ.get("EBO_AUDIO_RATE", "8000")
AUDIO_FILTER = os.environ.get("EBO_AUDIO_FILTER", "none")
TALK_ON = os.environ.get("EBO_TALK", "1") == "1"
SNAPSHOT_FPS = os.environ.get("EBO_SNAPSHOT_FPS", "2")


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"


class NativeRobotLink(RobotLink):
    def __init__(self):
        self.cred = load_credentials()
        self.ebo_dir = self.cred.ebo_dir
        self.snapshot_path = os.path.join(self.ebo_dir, "snapshot.jpg")
        # state
        self.frame_count = 0
        self.last_frame = 0.0
        self.audio_codec: int | None = None
        self.audio_flags: int | None = None
        self.audio_count = 0
        self.tx_audio_count = 0
        self.talk_available: bool | None = None
        self.connected = False
        self.codec_name: str | None = None
        self.battery = -1
        self.charge = -1
        self.attitude: dict | None = None     # {roll,pitch,yaw} if the robot streams ATTITUDE (msgid 30)
        self.imu: dict | None = None          # {ax,ay,az} if the robot streams RAW_IMU (msgid 27)
        self._seen_msgids: set[int] = set()    # for inbound-MAVLink discovery (logged once each)
        self.on_status: Callable[[int, int], None] | None = None
        self.toggles = {"eyes": None, "night": None, "avoid": None, "fall": None, "patrol": None}
        self.eyes_animation: str | None = None
        self.paused = False
        self._last_drive = 0.0
        self._running = False
        self._ff_primed = False
        self._started = False
        self._audio_sinks: list = []   # callbacks(mulaw_bytes): the voice skill and/or the 2-way call
        # control + audio pipes
        self._ctrl_r, self._ctrl_w = os.pipe()
        self._a_r, self._a_w = os.pipe()
        os.set_inheritable(self._a_r, True)
        os.set_blocking(self._a_w, False)
        self._a_buf = bytearray()
        self._a_lock = threading.Lock()
        self._ctrl_lock = threading.Lock()   # serialize fd3 writes (control + talkback)

    # ------------- lifecycle -------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._running = True
        if not self.cred.present:
            # Fail soft: report clearly but don't crash. The robot just won't connect.
            print(f"[native] WARNING: missing robot credentials: {self.cred.missing}. "
                  f"The robot will not connect. (mock mode is available for dev.)", flush=True)
        # Fail soft per subsystem (video, native link): if one can't start, log it and keep the app up so
        # the UI + telemetry still work. Mirrors Autobot's graceful-degradation philosophy.
        try:
            self._render_mediamtx()
            self._start_mediamtx()
            time.sleep(0.6)
            self._start_ffmpeg()
            threading.Thread(target=self._snapshotter, daemon=True).start()
        except Exception as e:  # noqa: BLE001
            print(f"[native] video pipeline failed to start: {type(e).__name__}: {e}", flush=True)
        try:
            self._start_bridge()
            threading.Thread(target=self._read_frames, daemon=True).start()
            threading.Thread(target=self._log_stderr, daemon=True).start()
            threading.Thread(target=self._drive_watchdog, daemon=True).start()
            if AUDIO_ON:
                threading.Thread(target=self._audio_feeder, daemon=True).start()
        except Exception as e:  # noqa: BLE001
            print(f"[native] native bridge failed to start: {type(e).__name__}: {e}", flush=True)

    def close(self) -> None:
        self._running = False
        for proc_attr in ("proc", "ff", "mtx"):
            p = getattr(self, proc_attr, None)
            if p is None:
                continue
            try:
                p.terminate(); p.wait(timeout=3)
            except Exception:
                try: p.kill()
                except Exception: pass

    # ------------- process management -------------
    def _render_mediamtx(self):
        tpl = os.path.join(self.ebo_dir, "mediamtx.template.yml")
        if not os.path.exists(tpl):
            tpl = str(Path(__file__).parent / "native" / "mediamtx.template.yml")
        out = os.path.join(self.ebo_dir, "mediamtx.yml")
        try:
            video.render_mediamtx_config(tpl, out)
        except Exception as e:  # noqa: BLE001
            print(f"[native] mediamtx config render failed: {e}", flush=True)

    def _start_mediamtx(self):
        self.mtx = subprocess.Popen(
            [os.path.join(self.ebo_dir, "mediamtx"), os.path.join(self.ebo_dir, "mediamtx.yml")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[native] mediamtx pid", self.mtx.pid, flush=True)

    def _start_ffmpeg(self):
        url = video.rtsp_url("127.0.0.1")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-analyzeduration", "500000", "-probesize", "1000000",
               "-thread_queue_size", "256", "-f", "hevc", "-i", "pipe:0"]
        pass_fds: tuple = ()
        if AUDIO_ON:
            if AUDIO_FILTER and AUDIO_FILTER.lower() != "none":
                aout = ["-af", AUDIO_FILTER, "-c:a", "libopus", "-application", "voip",
                        "-b:a", "24k", "-ar", "48000", "-ac", "1"]
            else:
                aout = ["-c:a", "copy"]
            cmd += ["-use_wallclock_as_timestamps", "1", "-thread_queue_size", "256",
                    "-f", AUDIO_FMT, "-ar", AUDIO_RATE, "-ac", "1", "-i", f"pipe:{self._a_r}",
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy"] + aout + ["-max_interleave_delta", "0"]
            pass_fds = (self._a_r,)
        else:
            cmd += ["-c:v", "copy", "-an"]
        cmd += ["-f", "rtsp", "-rtsp_transport", "tcp", url]
        self.ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, pass_fds=pass_fds)
        print("[native] ffmpeg pid", self.ff.pid, flush=True)

    def _restart_ffmpeg(self):
        try: self.ff.stdin.close()
        except Exception: pass
        try: self.ff.kill()
        except Exception: pass
        self._ff_primed = False
        self._start_ffmpeg()
        print("[native] ffmpeg restarted", flush=True)

    def _start_bridge(self):
        linker = os.path.join(self.cred.bionic_dir, "linker")
        binary = os.path.abspath(os.path.join(self.ebo_dir, "ebo_bridge"))
        env = dict(os.environ)
        env["EBO_LIB_DIR"] = self.cred.lib_dir
        env["EBO_IOCTL9930"] = self.cred.ioctl9930_path
        env["LD_LIBRARY_PATH"] = self.cred.bionic_dir + ":" + self.cred.lib_dir
        self.proc = subprocess.Popen([linker, binary], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, pass_fds=(self._ctrl_r,),
                                     preexec_fn=lambda: os.dup2(self._ctrl_r, 3))
        print("[native] bridge pid", self.proc.pid, flush=True)

    def pause_bridge(self):
        if self.paused:
            return
        self.paused = True
        self.connected = False
        print("[native] pausing bridge (releasing robot for the app)", flush=True)
        try: self.proc.terminate()
        except Exception: pass
        try: self.proc.wait(timeout=3)
        except Exception:
            try: self.proc.kill()
            except Exception: pass

    def resume_bridge(self):
        if not self.paused:
            return
        self.paused = False
        self._ff_primed = False
        print("[native] resuming bridge", flush=True)
        self._start_bridge()
        threading.Thread(target=self._read_frames, daemon=True).start()
        threading.Thread(target=self._log_stderr, daemon=True).start()

    # ------------- threads -------------
    def _audio_feeder(self):
        rate = int(AUDIO_RATE); chunk = max(80, rate // 50); interval = chunk / rate
        silence = (b"\xff" if AUDIO_FMT == "mulaw" else b"\xd5") * chunk
        next_t = time.monotonic()
        while self._running:
            next_t += interval
            with self._a_lock:
                if len(self._a_buf) >= chunk:
                    out = bytes(self._a_buf[:chunk]); del self._a_buf[:chunk]
                elif self._a_buf:
                    out = bytes(self._a_buf) + silence[len(self._a_buf):]; self._a_buf.clear()
                else:
                    out = silence
            try: os.write(self._a_w, out)
            except Exception: pass
            d = next_t - time.monotonic()
            if d > 0: time.sleep(d)
            else: next_t = time.monotonic()

    def _snapshotter(self):
        """Keep a current JPEG still on disk for snapshot() (the AI's vision input). Reads the published
        RTSP stream so we never touch the HEVC->ffmpeg path. Restarts if the stream isn't ready (asleep)."""
        url = video.rtsp_url("127.0.0.1")
        while self._running:
            if self.paused or not self.is_awake():
                time.sleep(1.0); continue
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                   "-i", url, "-vf", f"fps={SNAPSHOT_FPS}", "-q:v", "5", "-update", "1", "-y", self.snapshot_path]
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                while self._running and not self.paused and self.is_awake():
                    if p.poll() is not None:
                        break
                    time.sleep(0.5)
                try: p.terminate(); p.wait(timeout=2)
                except Exception:
                    try: p.kill()
                    except Exception: pass
            except Exception:
                pass
            time.sleep(1.0)

    def _log_stderr(self):
        for line in iter(self.proc.stderr.readline, b""):
            s = line.decode(errors="replace").rstrip()
            print("[bridge]", s, flush=True)
            if "connected" in s:
                self.connected = True
            if "talkback:" in s:
                self.talk_available = "rc=0" in s or "ok" in s.lower()
            if "talkback unavailable" in s:
                self.talk_available = False

    def _read_frames(self):
        f = self.proc.stdout
        while self._running:
            hdr = f.read(5)
            if len(hdr) < 5:
                break
            n = struct.unpack("<I", hdr[:4])[0]; codec = hdr[4]
            data = f.read(n)
            if len(data) < n:
                break
            if codec == 0xFF:
                self._parse_status(data)
                continue
            if codec == 0xA0:
                if len(data) >= 2:
                    self.audio_codec, self.audio_flags = data[0], data[1]
                    self.audio_count += 1
                    if len(data) > 2:
                        if AUDIO_ON:
                            with self._a_lock:
                                self._a_buf += data[2:]
                                if len(self._a_buf) > 1600:
                                    del self._a_buf[:len(self._a_buf) - 1600]
                        if self._audio_sinks:
                            payload = bytes(data[2:])                 # G.711 payload to each sink
                            for _sink in list(self._audio_sinks):
                                try: _sink(payload)
                                except Exception: pass
                continue
            self.frame_count += 1
            self.last_frame = time.time()
            self.codec_name = "hevc" if codec == 80 else ("h264" if codec == 78 else f"codec{codec}")
            if not self._ff_primed:
                if not video.has_param_set(data, codec):
                    continue
                self._ff_primed = True
            try:
                self.ff.stdin.write(data); self.ff.stdin.flush()
            except Exception:
                self._restart_ffmpeg()

    def _parse_status(self, u: bytes):
        i = 0
        while i + 8 <= len(u):
            if u[i] == 0xfe:
                plen = u[i + 1]; msgid = u[i + 5] if i + 5 < len(u) else -1
                p = i + 6   # payload start
                if msgid == 207 and i + 24 <= len(u):   # BATTERY_STATUS
                    bp = u[i + 22]; cs = u[i + 23]
                    if bp != self.battery or cs != self.charge:
                        self.battery = bp; self.charge = cs
                        if self.on_status:
                            try: self.on_status(bp, cs)
                            except Exception: pass
                elif msgid == 30 and p + 16 <= len(u):   # ATTITUDE: u32 time, f roll, f pitch, f yaw, ...
                    try:
                        roll, pitch, yaw = struct.unpack_from("<fff", u, p + 4)
                        self.attitude = {"roll": round(roll, 3), "pitch": round(pitch, 3), "yaw": round(yaw, 3)}
                    except Exception: pass
                elif msgid == 27 and p + 14 <= len(u):   # RAW_IMU: u64 time, i16 xacc,yacc,zacc, ...
                    try:
                        ax, ay, az = struct.unpack_from("<hhh", u, p + 8)
                        self.imu = {"ax": ax, "ay": ay, "az": az}
                    except Exception: pass
                elif msgid >= 0 and msgid not in self._seen_msgids and msgid != 207:
                    # Discovery: surface unhandled inbound message IDs once each so accelerometer/attitude
                    # (or other telemetry) can be identified on a given firmware. See docs/BRIDGE_PROTOCOL.md.
                    self._seen_msgids.add(msgid)
                    print(f"[native] inbound MAVLink msgid={msgid} len={plen} (unhandled — discovery)", flush=True)
                i += 8 + plen + 2 if plen else i + 1
            else:
                i += 1

    def _drive_watchdog(self):
        """Second safety layer: if /drive frames stop arriving, send a stop frame (~0.35s)."""
        while self._running:
            time.sleep(0.1)
            if self._last_drive and (time.time() - self._last_drive) > 0.35:
                self._last_drive = 0.0
                try: self.send_rdt(motor_frame())
                except Exception: pass

    # ------------- low-level send -------------
    def send_rdt(self, mavlink: bytes):
        with self._ctrl_lock:
            os.write(self._ctrl_w, struct.pack("<I", 1 + len(mavlink)) + b"\x00" + mavlink)

    def send_ioctl(self, io_type: int, data: bytes = b""):
        payload = b"\x01" + struct.pack("<H", io_type) + data
        with self._ctrl_lock:
            os.write(self._ctrl_w, struct.pack("<I", len(payload)) + payload)

    def send_audio(self, g711: bytes, codec_id: int = 0x8a) -> int:
        """Talkback: send outbound G.711 audio to the robot speaker via native control kind 2. Chunked into
        ~20ms frames (160 bytes @ 8kHz) so the native side can pace them."""
        if not TALK_ON:
            return 0
        chunk = 160
        sent = 0
        for off in range(0, len(g711), chunk):
            part = g711[off:off + chunk]
            payload = b"\x02" + bytes([codec_id & 0xFF]) + part
            try:
                with self._ctrl_lock:
                    os.write(self._ctrl_w, struct.pack("<I", len(payload)) + payload)
                sent += 1
            except Exception:
                break
        self.tx_audio_count += sent
        return sent

    # ------------- actions (sync, run off the event loop) -------------
    def is_awake(self) -> bool:
        return (time.time() - self.last_frame) < 6.0

    def _do_action(self, name: str) -> bool:
        if name == "dock":
            for _ in range(5): self.send_rdt(command_frame(CMD_DOCK)); time.sleep(0.15)
        elif name == "undock":
            for _ in range(6): self.send_rdt(motor_frame(ly=0.5)); time.sleep(0.05)
            self.send_rdt(motor_frame())
        elif name.startswith("eyes_") and name[5:] in EYE_ANIMATIONS:
            anim = name[5:]
            self.send_rdt(param_set_frame(EYE_ANIM_GROUP, EYE_ANIM_KEY, EYE_ANIMATIONS[anim]))
            self.eyes_animation = anim
            self.toggles["eyes"] = True
        elif name in PARAM_TOGGLES:
            g, k, v = PARAM_TOGGLES[name]; self.send_rdt(param_set_frame(g, k, v))
            self._track_toggle(name)
        else:
            return False
        return True

    def _track_toggle(self, name: str):
        for feat in ("eyes", "night", "avoid", "fall", "patrol"):
            if name == f"{feat}_on":
                self.toggles[feat] = True
            elif name == f"{feat}_off":
                self.toggles[feat] = False

    def _do_move(self, ly: float, rx: float, duration: float = 0.4):
        n = max(1, int(duration * 20))
        for _ in range(n):
            self.send_rdt(motor_frame(ly=ly, rx=rx)); time.sleep(0.05)
        self.send_rdt(motor_frame())

    def _do_drive(self, ly: float, rx: float):
        self.send_rdt(motor_frame(ly=ly, rx=rx))
        self._last_drive = time.time()

    def _do_stop(self):
        self._last_drive = 0.0
        for _ in range(3): self.send_rdt(motor_frame()); time.sleep(0.02)

    def _local_tts_pcm(self, text: str):
        """Best-effort espeak-ng -> 8kHz mono S16LE PCM (Pi-side fallback for /say {text})."""
        import shutil, tempfile, wave
        engine = "espeak-ng" if shutil.which("espeak-ng") else ("espeak" if shutil.which("espeak") else None)
        if not engine:
            return None
        fd, wav = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        try:
            subprocess.run([engine, "-w", wav, "-s", "150", text], check=True, capture_output=True, timeout=15)
            with wave.open(wav, "rb") as w:
                ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
                data = w.readframes(w.getnframes())
            if sw != 2:
                data = audioop.lin2lin(data, sw, 2)
            if ch == 2:
                data = audioop.tomono(data, 2, 0.5, 0.5)
            if sr != 8000:
                data, _ = audioop.ratecv(data, 2, 1, sr, 8000, None)
            return data
        except Exception:
            return None
        finally:
            try: os.remove(wav)
            except OSError: pass

    # ------------- async RobotLink interface -------------
    async def info(self) -> dict[str, Any]:
        ip = _lan_ip()
        return {
            "ok": True,
            "connected": self.connected, "paused": self.paused, "awake": self.is_awake(),
            "codec": self.codec_name, "frames_received": self.frame_count,
            "audio": ({"codec": f"0x{self.audio_codec:02x}", "flags": f"0x{self.audio_flags:02x}",
                       "count": self.audio_count} if self.audio_codec is not None else None),
            "battery": self.battery, "charge": self.charge,
            "rtsp": f"rtsp://{ip}:8554/{video.RTSP_PATH}",
        }

    async def telemetry(self) -> dict[str, Any]:
        return {
            "ok": True,
            "connected": self.connected, "paused": self.paused, "awake": self.is_awake(),
            "battery": self.battery, "charge": self.charge, "codec": self.codec_name,
            "frames_received": self.frame_count, "toggles": dict(self.toggles),
            "eyes_animation": self.eyes_animation, "eye_animations": sorted(EYE_ANIMATIONS.keys()),
            "audio_in": ({"codec": f"0x{self.audio_codec:02x}", "count": self.audio_count}
                         if self.audio_codec is not None else None),
            "audio_out": {"sent": self.tx_audio_count, "available": self.talk_available},
            "talk_enabled_bridge": TALK_ON,
            "attitude": self.attitude, "imu": self.imu,
            "rtsp": f"rtsp://{_lan_ip()}:8554/{video.RTSP_PATH}", "ts": time.time(),
        }

    async def snapshot(self) -> tuple[bytes | None, str | None]:
        def _read():
            p = Path(self.snapshot_path)
            if not p.exists() or not self.is_awake():
                return None, "asleep_or_not_ready"
            try:
                data = p.read_bytes()
            except Exception:
                return None, "snapshot unavailable"
            if len(data) < 100:
                return None, "snapshot not ready"
            return data, None
        return await asyncio.to_thread(_read)

    async def drive(self, ly: float, rx: float, *, generation: int | None = None,
                    epoch: int | None = None) -> dict[str, Any]:
        await asyncio.to_thread(self._do_drive, ly, rx)
        return {"ok": True}

    async def move(self, ly: float, rx: float, duration: float, *, generation: int | None = None,
                   epoch: int | None = None) -> dict[str, Any]:
        await asyncio.to_thread(self._do_move, ly, rx, duration)
        return {"ok": True}

    async def stop(self) -> dict[str, Any]:
        await asyncio.to_thread(self._do_stop)
        return {"ok": True}

    async def action(self, name: str) -> dict[str, Any]:
        ok = await asyncio.to_thread(self._do_action, name)
        return {"ok": True, "action": name} if ok else {"ok": False, "error": "unknown action"}

    async def connection(self, state: str) -> dict[str, Any]:
        if state == "stop":
            await asyncio.to_thread(self.pause_bridge)
        elif state == "start":
            await asyncio.to_thread(self.resume_bridge)
        else:
            return {"ok": False, "error": "unknown"}
        return {"ok": True, "paused": self.paused}

    async def say_audio(self, g711: bytes, codec: str = "mulaw") -> dict[str, Any]:
        if not TALK_ON:
            return {"ok": False, "error": "talkback disabled at bridge (EBO_TALK=0)"}
        codec_id = 0x8a if codec == "mulaw" else 0x8b
        sent = await asyncio.to_thread(self.send_audio, g711, codec_id)
        return {"ok": True, "frames": sent, "available": self.talk_available}

    async def say_text(self, text: str) -> dict[str, Any]:
        if not TALK_ON:
            return {"ok": False, "error": "talkback disabled at bridge (EBO_TALK=0)"}
        def _render_send():
            pcm = self._local_tts_pcm(text)
            if pcm is None:
                return None
            return self.send_audio(audioop.lin2ulaw(pcm, 2), 0x8a)
        sent = await asyncio.to_thread(_render_send)
        if sent is None:
            return {"ok": False, "error": "no local TTS on this host; render audio and use say_audio"}
        return {"ok": True, "frames": sent, "available": self.talk_available}

    # ------------- audio in (voice skill + 2-way call) -------------
    def set_audio_sink(self, callback) -> None:
        """Register a callback(mulaw_bytes) for inbound robot mic audio. Multiple sinks coexist (STT + call)."""
        if callback not in self._audio_sinks:
            self._audio_sinks.append(callback)

    # ------------- video upstreams (proxied by the web server) -------------
    @property
    def whep_upstream(self) -> str | None:
        return video.WHEP_UPSTREAM

    @property
    def hls_base(self) -> str | None:
        return video.HLS_BASE

    def stream_auth_header(self) -> dict[str, str]:
        return video.stream_auth_header()
