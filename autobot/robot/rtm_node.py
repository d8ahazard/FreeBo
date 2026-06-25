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
import itertools
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
        self._status_ts = 0.0                    # time.monotonic() when telemetry was last ACTUALLY received
        self.connected = False
        self.last_error: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._logs: deque[str] = deque(maxlen=300)
        # --- command acknowledgment (P0-R3.1): correlate each acked command to the sidecar's real Agora send
        #     result, so callers learn ACTUAL delivery instead of stdin-write success. ---
        self._cmd_ids = itertools.count(1)
        self._pending: dict[int, dict] = {}      # command_id -> {event, result}
        self._pending_lock = threading.Lock()
        self.last_ok_send: Optional[dict] = None
        self.last_fail_send: Optional[dict] = None
        self.last_command_id: Optional[int] = None
        self.last_ack_ms: Optional[float] = None
        self.consecutive_send_failures = 0
        # --- E-STOP generation reconciliation (P0-R4.4) ---
        # AUTHORITATIVE (what Python/SafetyFloor commands): re-asserted to the sidecar on every (re)connect so a
        # restarted sidecar can never accept motion under the wrong latch/generation. SIDECAR (echoed back in
        # command_result) is what the Node side actually holds; a mismatch must block motion.
        self._auth_gen = 0
        self._auth_latched = False
        self._sidecar_gen = 0
        self._sidecar_latched = True   # unknown until the sidecar echoes its state -> assume NOT safe to move
        self._last_reconcile_error: Optional[str] = None

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

    def _fail_pending(self, reason: str) -> None:
        """Release every blocked send_acked waiter immediately (P0-R4.4) so a sidecar exit/reconnect can't
        leave a caller hung until its timeout. The synthetic result reports a non-delivery."""
        with self._pending_lock:
            slots = list(self._pending.items())
            self._pending.clear()
        for _cid, slot in slots:
            slot["result"] = {"ev": "command_result", "sent_to_agora": False, "error": reason}
            slot["event"].set()

    def _kill(self) -> None:
        p = self._proc
        self._proc = None
        self.connected = False
        self._sidecar_latched = True   # unknown sidecar -> assume NOT safe to move until reconciled
        self._fail_pending("sidecar exited/reconnecting")
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

    def status_age(self) -> float:
        """Seconds since telemetry was ACTUALLY received from the robot (monotonic source-update time, not a
        request time). float('inf') until the first telemetry arrives. Used for HOLD-on-stale-telemetry."""
        return float("inf") if self._status_ts == 0.0 else (time.monotonic() - self._status_ts)

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
            self._status_ts = time.monotonic()   # genuine source-update time (NOT a request time)
        elif t == "command_result":
            # Learn the sidecar's authoritative latch/generation from every echo (P0-R4.4).
            if ev.get("generation") is not None:
                self._sidecar_gen = int(ev.get("generation"))
            if ev.get("latched") is not None:
                self._sidecar_latched = bool(ev.get("latched"))
            cid = ev.get("command_id")
            if cid is not None:
                with self._pending_lock:
                    slot = self._pending.get(cid)
                # Duplicate / out-of-order results find no slot (already popped) and are harmlessly ignored.
                if slot is not None:
                    slot["result"] = ev
                    slot["event"].set()
        elif t == "stat":
            self.consecutive_send_failures = int(ev.get("consecutive_send_failures", 0) or 0)
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
        # P0-R4.4: re-assert the authoritative latch+generation so a freshly (re)started sidecar — which
        # defaults to motion-blocked — can only accept motion under the state Python actually commands.
        self._send({"cmd": "set_control", "generation": self._auth_gen, "latched": self._auth_latched})

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

    def set_control(self, generation: int, latched: bool) -> bool:
        """Push the authoritative latch + generation to the sidecar (P0-R4.4). Stored so it is re-asserted on
        every (re)connect."""
        self._auth_gen = int(generation)
        self._auth_latched = bool(latched)
        return self._send({"cmd": "set_control", "generation": self._auth_gen, "latched": self._auth_latched})

    def reset_control(self, generation: int, timeout: float = 0.8) -> dict:
        """RESET that FAILS CLOSED (P0-R4 item 4). Desired latch stays True until a correlated estop_reset
        response is fully validated: ok True, latched False, matching generation, rtm_connected True,
        control_ready True. The request carries `expected_generation` (the desired gen we're resetting from) so
        a newer STOP that advanced the sidecar generation rejects this reset. Only on full validation do we
        commit the desired state to unlatched. Any exception/timeout/None/missing field leaves it latched."""
        expected = self._auth_gen
        res = self.send_acked({"cmd": "estop_reset", "expected_generation": expected,
                               "generation": int(generation)}, timeout)
        ok = (res.get("ok") is True and res.get("latched") is False
              and res.get("generation") == int(generation)
              and res.get("rtm_connected") is True and res.get("control_ready") is True)
        if ok:
            self._auth_latched = False
            self._auth_gen = int(generation)
            self._last_reconcile_error = None
        else:
            self._last_reconcile_error = res.get("error") or "reset not validated (fail-closed)"
        return {**res, "ok": ok, "reconciled": ok,
                "error": (None if ok else self._last_reconcile_error)}

    def control_state(self) -> dict:
        """Process vs sidecar latch/generation for the readiness surface (P0-R4.4). `synchronized` False means
        the two disagree (e.g. a sidecar restart that hasn't been reconciled) — motion must be blocked."""
        synced = (self._auth_gen == self._sidecar_gen) and (self._auth_latched == self._sidecar_latched)
        return {"process_latched": self._auth_latched, "process_generation": self._auth_gen,
                "sidecar_latched": self._sidecar_latched, "sidecar_generation": self._sidecar_gen,
                "synchronized": synced, "last_reconcile_error": self._last_reconcile_error}

    # --- acknowledged commands (real Agora delivery, not pipe-write) ---
    def send_acked(self, cmd: dict, timeout: float = 1.5) -> dict:
        """Send a command and BLOCK (bounded) for the sidecar's correlated command_result, returning ACTUAL
        Agora delivery. Call off the event loop (asyncio.to_thread). Returns:
        {ok, queued_to_sidecar, sent_to_agora, command_id, ack_ms, rtm_connected, error}."""
        cid = next(self._cmd_ids)
        out = {"ok": False, "queued_to_sidecar": False, "sent_to_agora": False, "command_id": cid,
               "ack_ms": None, "rtm_connected": self.connected, "error": None}
        # P0-R4.4: track the authoritative latch/generation as STOP/RESET flow through here, and stamp the
        # current generation onto every drive so the sidecar can reject a STALE (pre-STOP) drive that arrives
        # late. estop/estop_reset carry Python's authoritative generation so the sidecar adopts it.
        kind = cmd.get("cmd")
        if kind == "estop":
            # STOP commits the desired latch IMMEDIATELY (fail-safe), before the ack.
            self._auth_latched = True
            if cmd.get("generation") is not None:
                self._auth_gen = int(cmd["generation"])
        elif kind == "drive" and "generation" not in cmd:
            cmd = {**cmd, "generation": self._auth_gen}
        # NOTE (P0-R4 item 4): estop_reset does NOT mutate desired state here. The desired latch is cleared
        # only by reset_control() AFTER the sidecar response is validated — so a failed/partial reset fails
        # closed (stays latched).
        evt = threading.Event()
        with self._pending_lock:
            self._pending[cid] = {"event": evt, "result": None}
        t0 = time.monotonic()
        if not self._send({**cmd, "command_id": cid}):
            with self._pending_lock:
                self._pending.pop(cid, None)
            out["error"] = "sidecar stdin unavailable"
            self.last_fail_send = out
            return out
        out["queued_to_sidecar"] = True
        got = evt.wait(timeout)
        with self._pending_lock:
            slot = self._pending.pop(cid, None)
        res = (slot or {}).get("result")
        out["ack_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        out["rtm_connected"] = self.connected
        self.last_command_id = cid
        self.last_ack_ms = out["ack_ms"]
        if not got or res is None:
            out["error"] = "ack timeout"
            self.last_fail_send = {**out, "cmd": cmd.get("cmd")}
            return out
        out["sent_to_agora"] = bool(res.get("sent_to_agora"))
        out["ok"] = out["sent_to_agora"]
        out["error"] = res.get("error")
        # Surface the sidecar's honest extra facts (P0-R4 item 2/4) when present, so callers report local
        # safety vs transport independently and can validate a reset response.
        for k in ("initial_zero_sdk_send_succeeded", "local_latch_set", "retry_count", "latched",
                  "generation", "control_ready", "rtm_connected"):
            if k in res:
                out[k] = res[k]
        rec = {**out, "cmd": cmd.get("cmd")}
        if out["ok"]:
            self.last_ok_send = rec
        else:
            self.last_fail_send = rec
        return out

    def debug(self) -> dict:
        with self._pending_lock:
            pending = len(self._pending)
        return {"rtm_connected": self.connected, "last_error": self.last_error,
                "last_command_id": self.last_command_id, "last_ack_ms": self.last_ack_ms,
                "pending": pending, "consecutive_send_failures": self.consecutive_send_failures,
                "last_ok_send": self.last_ok_send, "last_fail_send": self.last_fail_send,
                "control_state": self.control_state(),
                "status_age_s": round(self.status_age(), 2) if self._status_ts else None,
                "recent_logs": list(self._logs)[-20:]}

    def recent_logs(self) -> list[str]:
        return list(self._logs)
