"""Onboarding orchestration — the backend behind the first-run wizard (the user-facing spine).

Sequences the bring-up the wizard walks through:
  2. enable ADB (wired USB or wireless)         -> autobot/web/adb.py
  3. install the patched app + capture creds     -> start receiver + adb reverse + install + launch, then stop
  4. talk to the robot by brand, verify connect  -> connect_test()
  6. pair the robot to the owner                 -> set_owner() (+ optional face enroll)

Step 1 (the UI) and step 5 (pick the brain) are handled by the existing /api/setup flow.

IMPORTANT: the collector internals are owned by another agent (see docs/TODO-FREEBO.md). This module only
*orchestrates* a contract: it runs the existing `collector/receiver.py` (which writes the top-level .env +
vendor/ioctl9930.bin), drives adb, and tracks the capture window. The patched-APK behavior (emit creds for
~5 min on start, or until a stop flag) and the receiver's stop endpoint are the collector's to implement; we
call them and degrade gracefully when absent. No secrets are read or logged in full here (values are masked).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import adb

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"
VENDOR_DIR = REPO_ROOT / "vendor"

CAPTURE_WINDOW_S = 300       # the patched app emits creds for ~5 min on start (per the agreed model)
CRED_FIELDS = ["EBO_LICENSE", "EBO_UID", "EBO_AUTHKEY", "EBO_IDENTITY", "EBO_TOKEN"]

# The patched capture app (built by collector/apk_patch) logs creds to "AUTOBOT*" logcat tags. Defaults
# point at the all-in-one build; override via env if your build differs.
PATCHED_APK = Path(os.environ.get("EBO_PATCHED_APK",
                                  str(REPO_ROOT / "collector" / "apk_patch" / "build" / "ebohome-secret.apk")))
APP_PACKAGE = os.environ.get("EBO_APP_PACKAGE", "com.enabot.ebox.intl")
CAPTURE_TAGS = ["AUTOBOTuid", "AUTOBOTacct", "AUTOBOTpass", "AUTOBOTsecret", "AUTOBOTbodykey",
                "AUTOBOTcookie", "AUTOBOT_HTTP"]
# logcat tag -> .env key (cookie/http are parsed specially)
TAG_TO_ENV = {"uid": "EBO_UID", "acct": "EBO_IDENTITY", "pass": "EBO_TOKEN",
              "secret": "EBO_SIGN_SECRET", "bodykey": "EBO_BODY_KEY"}
_LINE_RE = re.compile(r"AUTOBOT(\w+)\(\s*\d+\):\s*(.*?)\s*$")


def _mask(v: str) -> str:
    return "" if not v else ("***" if len(v) <= 6 else f"{v[:2]}…{v[-2:]}")


def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


class Onboarding:
    def __init__(self, link, settings, identity, brain, emit):
        self.link = link
        self.settings = settings
        self.identity = identity
        self.brain = brain
        self.emit = emit
        self._capture_started: float = 0.0
        self._serial: str = ""
        self._captured: dict[str, str] = {}      # raw captured values (env-key -> value)
        self._logcat: Optional[subprocess.Popen] = None
        self._logcat_thread: Optional[threading.Thread] = None
        self._robot_id: str = os.environ.get("EBO_ROBOT_ID", "")

    # ---------------- step 2: ADB ----------------
    def adb_status(self) -> dict[str, Any]:
        # adb auto-installs (Google platform-tools) on first use — no manual setup.
        if not adb.available():
            return {"ok": True, "adb": False, "devices": [],
                    "note": "Couldn't auto-install adb (no internet, or this is ARM Linux — run "
                            "`apt install android-tools-adb`). You can also use the in-browser USB (WebADB) option."}
        d = adb.devices()
        return {"ok": True, "adb": True, "devices": d.get("devices", []), "error": d.get("error")}

    def adb_pair(self, host_port: str, code: str) -> dict[str, Any]:
        return adb.pair(host_port, code)

    def adb_connect(self, host_port: str) -> dict[str, Any]:
        return adb.connect(host_port)

    # ---------------- step 3: capture (install patched app + read creds from logcat) ----------------
    def _pick_serial(self) -> str:
        d = adb.devices().get("devices", [])
        ready = [x for x in d if x.get("state") == "device"]
        # prefer a wireless (IP:port) transport
        for x in ready:
            if ":" in x.get("serial", ""):
                return x["serial"]
        return ready[0]["serial"] if ready else ""

    def capture_start(self, serial: str = "", apk_path: str = "", package: str = "", port: int = 8400) -> dict[str, Any]:
        """Install the patched capture app, launch it, and start reading creds from logcat.

        The patched app logs license/uid/identity/token + the cloud sign secret + the login session cookie to
        `AUTOBOT*` logcat tags. We install it, clear the log, launch it, then stream + parse those tags into
        `.env`. The user just opens the app and connects to the robot once."""
        adbp = adb.ensure_adb()
        if not adbp:
            return {"ok": False, "error": "adb unavailable (could not auto-install)"}
        self._serial = serial or self._pick_serial()
        if not self._serial:
            return {"ok": False, "error": "no phone detected over adb — connect it (wired or wireless) first"}
        apk = apk_path or str(PATCHED_APK)
        pkg = package or APP_PACKAGE
        steps: list[dict] = []
        if os.path.isfile(apk):
            steps.append({"step": "install", **adb.install(apk, self._serial)})
        else:
            steps.append({"step": "install", "ok": False, "note": f"patched APK not found at {apk}"})
        # fresh log, launch app, begin streaming the AUTOBOT* tags
        adb._run(["-s", self._serial, "logcat", "-c"])  # noqa: SLF001 - intentional reuse
        steps.append({"step": "launch", **adb.start_app(pkg, serial=self._serial)})
        self._captured.clear()
        self._capture_started = time.time()
        self._start_logcat(adbp)
        return {"ok": True, "steps": steps, "window_s": CAPTURE_WINDOW_S,
                "instruction": "On your phone, open the EBO app and connect to your robot once.",
                **self.capture_status()}

    def _start_logcat(self, adbp: str) -> None:
        if self._logcat and self._logcat.poll() is None:
            return
        args = [adbp, "-s", self._serial, "logcat", "-v", "brief"] + [f"{t}:V" for t in CAPTURE_TAGS] + ["*:S"]
        try:
            self._logcat = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            text=True, errors="replace", bufsize=1)
        except Exception as e:  # noqa: BLE001
            print(f"[onboarding] logcat start failed: {e}", flush=True)
            return
        self._logcat_thread = threading.Thread(target=self._read_logcat, daemon=True)
        self._logcat_thread.start()

    def _read_logcat(self) -> None:
        proc = self._logcat
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            if "AUTOBOT" not in line:
                continue
            self._ingest(line)

    def _ingest(self, line: str) -> None:
        m = _LINE_RE.search(line)
        if not m:
            return
        tag, val = m.group(1), m.group(2).strip()
        changed = False
        if tag in TAG_TO_ENV and val:
            if self._captured.get(TAG_TO_ENV[tag]) != val:
                self._captured[TAG_TO_ENV[tag]] = val
                changed = True
        elif tag == "cookie" and val:
            try:
                for c in json.loads(val):
                    if c.get("name") == "sessionid" and c.get("value"):
                        if self._captured.get("EBO_SESSION_COOKIE") != c["value"]:
                            self._captured["EBO_SESSION_COOKIE"] = c["value"]; changed = True
            except Exception:  # noqa: BLE001
                pass
        elif tag == "_HTTP" and not self._robot_id:
            rid = re.search(r"robot_members/(\d+)|\"robot_id\":(\d+)", val)
            if rid:
                self._robot_id = rid.group(1) or rid.group(2)
                self._captured["EBO_ROBOT_ID"] = self._robot_id; changed = True
        if changed:
            self._write_env()

    def _write_env(self) -> None:
        env = _read_env()
        env.update(self._captured)
        lines = ["# FreeBo creds — collector-captured. Do NOT commit. (.env is gitignored)"]
        lines += [f"{k}={v}" for k, v in env.items()]
        try:
            ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def capture_status(self) -> dict[str, Any]:
        env = _read_env()
        # what we actually got this session (merged with .env)
        got = dict(env); got.update(self._captured)
        # Two ways to be "ready": cloud (Air 2/Max) = sign secret + session cookie; or TUTK (SE) creds.
        cloud_ready = bool(got.get("EBO_SIGN_SECRET") and got.get("EBO_SESSION_COOKIE"))
        tutk_ready = all(got.get(f) for f in ("EBO_UID", "EBO_IDENTITY", "EBO_TOKEN"))
        show = ["EBO_UID", "EBO_IDENTITY", "EBO_TOKEN", "EBO_SIGN_SECRET", "EBO_SESSION_COOKIE", "EBO_ROBOT_ID"]
        fields = {k: _mask(got.get(k, "")) for k in show}
        remaining = max(0.0, CAPTURE_WINDOW_S - (time.time() - self._capture_started)) if self._capture_started else 0.0
        return {"ok": True, "fields": fields, "cloud_ready": cloud_ready, "tutk_ready": tutk_ready,
                "complete": cloud_ready or tutk_ready, "window_remaining": round(remaining, 1),
                "capturing": bool(self._logcat and self._logcat.poll() is None)}

    def capture_stop(self) -> dict[str, Any]:
        if self._logcat and self._logcat.poll() is None:
            try:
                self._logcat.terminate()
            except Exception:  # noqa: BLE001
                pass
        if self._serial and adb.available():
            adb.force_stop(APP_PACKAGE, self._serial)
        self._capture_started = 0.0
        return {"ok": True, **self.capture_status()}

    # ---------------- step 4: connection self-test ----------------
    async def connect_test(self) -> dict[str, Any]:
        env = _read_env()
        creds_present = all(env.get(f) for f in CRED_FIELDS) and (VENDOR_DIR / "ioctl9930.bin").is_file()
        try:
            t = await self.link.telemetry()
        except Exception as e:  # noqa: BLE001
            t = {"connected": False, "error": str(e)}
        s = self.settings.snapshot()
        connected = bool(t.get("connected"))
        hint = None
        if not creds_present:
            hint = "Capture robot credentials first (step 3)."
        elif not connected and s.robot_link != "mock":
            hint = ("Credentials present but not connected. Make sure the Enabot app is closed and the robot "
                    "is awake, then restart FreeBo so the link connects with the new credentials.")
        return {"ok": True, "variant": s.robot_variant, "robot_link": s.robot_link,
                "creds_present": creds_present, "connected": connected,
                "frames": t.get("frames_received", 0), "battery": t.get("battery", -1),
                "untested": bool(t.get("untested")), "hint": hint}

    # ---------------- step 6: owner pairing ----------------
    async def set_owner(self, name: str, enroll: bool = False) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "owner name required"}
        self.settings.update(owner_name=name)
        result: dict[str, Any] = {"ok": True, "owner": name, "enrolled": False}
        if enroll:
            try:
                jpeg, _ = await self.link.snapshot()
                if jpeg:
                    self.brain.ctx.flags["last_jpeg"] = jpeg
                res = await self.brain.registry.execute("enroll_face", {"name": name})
                result["enrolled"] = bool(res.get("ok"))
                result["enroll_detail"] = res
            except Exception as e:  # noqa: BLE001
                result["enroll_detail"] = {"ok": False, "error": str(e),
                                           "note": "face recognition optional (pip install face_recognition)"}
        if self.emit:
            await self.emit({"type": "settings", "changed": ["owner_name"],
                             "settings": self.settings.public_dict()})
        return result
