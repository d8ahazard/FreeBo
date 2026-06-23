"""ADB abstraction for the onboarding wizard — wired (USB) and wireless provisioning.

Thin, fail-soft wrapper around the host `adb` binary used by autobot/web/onboarding.py to: list devices,
pair + connect over wireless ADB, install the (collector-provided) patched APK, set up an `adb reverse`
loopback tunnel for credential capture, and launch/stop the app. Everything returns a JSON-able dict and
never raises, so the wizard degrades gracefully when adb or a device is missing.

This file does NOT contain any capture logic or secrets — it only drives adb. The patched APK and the
capture protocol are owned by the collector (see docs/TODO-FREEBO.md). A browser WebUSB/WebADB path also
exists in the UI (webui) for hosts with no adb installed.
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Optional

# Local cache for an auto-downloaded adb so the user never has to install platform-tools by hand.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADB_CACHE = Path(os.environ.get("FREEBO_ADB_DIR", str(_REPO_ROOT / "data" / "platform-tools")))
_DL_BASE = "https://dl.google.com/android/repository/platform-tools-latest-"
_dl_lock = threading.Lock()


def _exe_name() -> str:
    return "adb.exe" if platform.system() == "Windows" else "adb"


def _resolve_adb() -> Optional[str]:
    """Find adb WITHOUT downloading: ADB_PATH env, the local cache, then PATH."""
    p = os.environ.get("ADB_PATH")
    if p and os.path.isfile(p):
        return p
    cached = _ADB_CACHE / _exe_name()
    if cached.is_file():
        return str(cached)
    return shutil.which("adb")


def _platform_zip() -> Optional[str]:
    sysname = platform.system()
    machine = platform.machine().lower()
    # Google publishes x86_64 builds only; on ARM Linux (e.g. a Pi) use the system adb instead.
    if sysname == "Linux" and ("arm" in machine or "aarch64" in machine):
        return None
    return {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}.get(sysname)


def ensure_adb() -> Optional[str]:
    """Return a usable adb path, downloading Google's platform-tools into the local cache if needed.
    100% free, no manual install. Returns None only if it truly can't be provided (e.g. ARM Linux w/o adb)."""
    found = _resolve_adb()
    if found:
        return found
    zip_os = _platform_zip()
    if not zip_os:
        return None  # ARM Linux etc. — caller should suggest `apt install android-tools-adb`
    with _dl_lock:
        found = _resolve_adb()  # re-check inside the lock
        if found:
            return found
        try:
            _ADB_CACHE.parent.mkdir(parents=True, exist_ok=True)
            url = f"{_DL_BASE}{zip_os}.zip"
            print(f"[adb] downloading platform-tools ({zip_os}) ...", flush=True)
            tmp_zip = _ADB_CACHE.parent / "_platform-tools.zip"
            with urllib.request.urlopen(url, timeout=120) as r, open(tmp_zip, "wb") as f:  # noqa: S310
                shutil.copyfileobj(r, f)
            with zipfile.ZipFile(tmp_zip) as z:
                z.extractall(_ADB_CACHE.parent)   # extracts a "platform-tools/" dir
            tmp_zip.unlink(missing_ok=True)
            adb = _ADB_CACHE / _exe_name()
            if adb.is_file() and platform.system() != "Windows":
                adb.chmod(adb.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            print(f"[adb] installed -> {adb}", flush=True)
            return str(adb) if adb.is_file() else None
        except Exception as e:  # noqa: BLE001
            print(f"[adb] auto-install failed: {e}", flush=True)
            return None


def adb_path() -> Optional[str]:
    """adb path if present (no download). Use ensure_adb() to auto-install."""
    return _resolve_adb()


def available() -> bool:
    return ensure_adb() is not None


def _run(args: list[str], timeout: float = 30.0) -> dict[str, Any]:
    adb = ensure_adb()
    if not adb:
        return {"ok": False, "error": "adb unavailable (auto-install failed; on a Pi run "
                                       "`apt install android-tools-adb`)"}
    try:
        p = subprocess.run([adb, *args], capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        return {"ok": p.returncode == 0, "rc": p.returncode, "out": out, "err": err}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"adb {args[0] if args else ''} timed out"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def devices() -> dict[str, Any]:
    r = _run(["devices", "-l"])
    if not r.get("ok"):
        return {"ok": False, "devices": [], "error": r.get("error") or r.get("err")}
    devs = []
    for line in r["out"].splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial, state = parts[0], (parts[1] if len(parts) > 1 else "unknown")
        devs.append({"serial": serial, "state": state, "wireless": ":" in serial})
    return {"ok": True, "devices": devs}


def pair(host_port: str, code: str) -> dict[str, Any]:
    """Wireless ADB pairing (Android 11+). host_port like '192.168.1.5:37123', code is the 6-digit pin."""
    if not host_port or not code:
        return {"ok": False, "error": "host:port and pairing code required"}
    adb = ensure_adb()
    if not adb:
        return {"ok": False, "error": "adb unavailable"}
    try:
        p = subprocess.run([adb, "pair", host_port], input=f"{code}\n", capture_output=True, text=True, timeout=30)
        ok = p.returncode == 0 and "ailed" not in (p.stdout + p.stderr).lower()
        return {"ok": ok, "out": (p.stdout or "").strip(), "err": (p.stderr or "").strip()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def connect(host_port: str) -> dict[str, Any]:
    return _run(["connect", host_port])


def disconnect(host_port: str = "") -> dict[str, Any]:
    return _run(["disconnect", *( [host_port] if host_port else [])])


def _target(serial: str) -> list[str]:
    return ["-s", serial] if serial else []


def install(apk_path: str, serial: str = "") -> dict[str, Any]:
    if not apk_path or not os.path.isfile(apk_path):
        return {"ok": False, "error": f"apk not found: {apk_path}"}
    return _run([*_target(serial), "install", "-r", "-g", apk_path], timeout=180)


def uninstall(package: str, serial: str = "") -> dict[str, Any]:
    return _run([*_target(serial), "uninstall", package])


def reverse(remote: str, local: str, serial: str = "") -> dict[str, Any]:
    """adb reverse remote local — e.g. reverse('tcp:8400','tcp:8400') so the phone reaches the host receiver
    on loopback (creds never traverse wifi)."""
    return _run([*_target(serial), "reverse", remote, local])


def reverse_remove_all(serial: str = "") -> dict[str, Any]:
    return _run([*_target(serial), "reverse", "--remove-all"])


def start_app(package: str, activity: str = "", serial: str = "") -> dict[str, Any]:
    if activity:
        return _run([*_target(serial), "shell", "am", "start", "-n", f"{package}/{activity}"])
    return _run([*_target(serial), "shell", "monkey", "-p", package, "-c",
                 "android.intent.category.LAUNCHER", "1"])


def force_stop(package: str, serial: str = "") -> dict[str, Any]:
    return _run([*_target(serial), "shell", "am", "force-stop", package])
