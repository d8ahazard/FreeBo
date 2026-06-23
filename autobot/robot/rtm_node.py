"""RtmNode — Python manager for the headless Agora RTM sidecar (scripts/rtm_sidecar.js).

This is how FreeBo drives the EBO Air 2 cloud control plane NATIVELY, with no browser: a small Node process
runs the real Agora RTM SDK headless and we speak to it over newline-delimited JSON on its stdin/stdout.

We own the lifecycle: spawn the sidecar, fetch + push a session (via a provider), forward control commands
(drive/stop/eyes/dock/avoid/raw), ingest inbound robot messages (battery/charge/IMU/TOF/touch, drive-reject),
and reconnect — re-fetching a fresh session when the sidecar reports its token expired. A background reader
thread parses stdout events; stderr (SDK logs) is drained separately so the pipe never blocks.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
SIDECAR = REPO_ROOT / "scripts" / "rtm_sidecar.js"

SessionProvider = Callable[[], Awaitable[dict]]


def _node_bin() -> Optional[str]:
    return os.environ.get("AUTOBOT_NODE_BIN") or shutil.which("node")


class RtmNode:
    def __init__(self, session_provider: SessionProvider,
                 on_event: Optional[Callable[[dict], None]] = None) -> None:
        self.session_provider = session_provider
        self.on_event = on_event
        self.status: dict[str, Any] = {}        # latest battery/charge/sensors merged from inbound peer msgs
        self.connected = False
        self.last_error: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._logs: deque[str] = deque(maxlen=300)

    # --- lifecycle ---
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="rtm-node", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._kill()

    def _kill(self) -> None:
        p = self._proc
        self._proc = None
        self.connected = False
        if p:
            try:
                if p.stdin:
                    p.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        node = _node_bin()
        if not node:
            self.last_error = "node not found (install Node.js or set AUTOBOT_NODE_BIN)"
            return
        if not SIDECAR.is_file():
            self.last_error = f"sidecar missing: {SIDECAR}"
            return
        backoff = 1.0
        while self._running:
            try:
                self._serve(node)
                backoff = 1.0
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
            self.connected = False
            if not self._running:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    def _kill_stale_sidecars(self) -> None:
        """Kill any leftover rtm_sidecar node processes BEFORE spawning ours. Critical: every stale sidecar is
        still logged into RTM with the SAME uid -> uid conflict -> all sends fail 'not logged in' (this was
        the 'drops motion control regularly' bug). Guarantees exactly one logged-in controller."""
        try:
            import psutil
            for p in psutil.process_iter(["name", "cmdline"]):
                try:
                    cl = " ".join(p.info.get("cmdline") or [])
                    if "rtm_sidecar" in cl:
                        p.kill()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 - psutil missing: best-effort OS fallback
            import sys
            try:
                if sys.platform.startswith("win"):
                    subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "Get-CimInstance Win32_Process -Filter \"name='node.exe'\" | "
                                    "Where-Object { $_.CommandLine -like '*rtm_sidecar*' } | "
                                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                                   capture_output=True, timeout=10)
                else:
                    subprocess.run(["pkill", "-f", "rtm_sidecar"], capture_output=True, timeout=10)
            except Exception:  # noqa: BLE001
                pass

    def _serve(self, node: str) -> None:
        self._kill_stale_sidecars()
        self._proc = subprocess.Popen(
            [node, str(SIDECAR)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, cwd=str(REPO_ROOT))
        threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True).start()
        self._connect()  # push an initial session
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            if not self._running:
                break
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            self._handle_event(ev)

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                self._logs.append(line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    # --- events from the sidecar ---
    def _handle_event(self, ev: dict) -> None:
        t = ev.get("ev")
        if t == "state":
            self.connected = ev.get("state") == "CONNECTED"
        elif t == "connected":
            self.connected = True
            self.status["connected"] = True
        elif t == "need_session":
            self._connect(force=True)   # token likely expired -> must re-login with a FRESH session
        elif t == "peer":
            p = ev.get("parsed") or {}
            for k, v in p.items():
                if k == "id" or v is None:
                    continue
                self.status[k] = v
            self.status["connected"] = True
        elif t == "log":
            self._logs.append(f"[{ev.get('level')}] {ev.get('msg')}")
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:  # noqa: BLE001
                pass

    def _connect(self, force: bool = False) -> None:
        """Fetch a session (forced-fresh on re-provision) and hand it to the sidecar to (re)login."""
        try:
            try:
                sess = asyncio.run(self.session_provider(force))   # provider that supports force-fresh
            except TypeError:
                sess = asyncio.run(self.session_provider())        # plain provider (tests)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"session: {type(e).__name__}: {e}"
            return
        if not isinstance(sess, dict) or not sess.get("ok"):
            self.last_error = f"session not ok: {sess}"
            return
        self._send({"cmd": "connect", "session": sess})

    # --- commands to the sidecar ---
    def _send(self, cmd: dict) -> bool:
        with self._lock:
            p = self._proc
            if not p or not p.stdin:
                return False
            try:
                p.stdin.write(json.dumps(cmd) + "\n")
                p.stdin.flush()
                return True
            except Exception:  # noqa: BLE001
                return False

    def drive(self, ly: float, rx: float, duration: float = 0.0) -> bool:
        return self._send({"cmd": "drive", "ly": ly, "rx": rx, "duration": duration})

    def stop(self) -> bool:
        return self._send({"cmd": "stop"})

    def eyes(self, state: str) -> bool:
        return self._send({"cmd": "eyes", "state": state})

    def dock(self) -> bool:
        return self._send({"cmd": "dock"})

    def avoid(self, on: bool = True) -> bool:
        return self._send({"cmd": "avoid", "on": on})

    def raw(self, msg_id: int, data: dict | None = None) -> bool:
        return self._send({"cmd": "raw", "id": msg_id, "data": data or {}})

    def recent_logs(self) -> list[str]:
        return list(self._logs)
