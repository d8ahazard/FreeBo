#!/usr/bin/env python3
"""FreeBo one-command launcher — clone the repo, run this, get a working UI.

Cross-platform (Windows / macOS / Linux / Pi). It:
  1. creates a local virtualenv (.venv) if missing,
  2. installs Python deps (requirements.txt),
  3. builds the web UI (webui/dist) if it's missing and npm is available (else the server serves
     autobot/web/static/fallback.html),
  4. best-effort builds the libeboproto shared lib (for the ctypes protocol path) if a C toolchain exists,
  5. launches `python -m autobot` and opens the browser to the dashboard.

Robot link is auto-selected by autobot/config.py (mock off a robot box). Set AUTOBOT_ROBOT_LINK to force it.

Usage:  python scripts/bootstrap.py [--no-venv] [--no-ui-build] [--no-browser] [--port 8200]
The thin wrappers start.ps1 / start.sh just call this.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IS_WIN = platform.system() == "Windows"


def log(msg: str) -> None:
    print(f"[freebo] {msg}", flush=True)


def venv_python(venv_dir: str) -> str:
    return os.path.join(venv_dir, "Scripts", "python.exe") if IS_WIN else os.path.join(venv_dir, "bin", "python")


def ensure_venv() -> str:
    venv_dir = os.path.join(REPO, ".venv")
    py = venv_python(venv_dir)
    if not os.path.isfile(py):
        log("creating virtualenv (.venv) ...")
        subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
    return py


def install_deps(py: str) -> None:
    req = os.path.join(REPO, "requirements.txt")
    sentinel = os.path.join(REPO, ".venv", ".deps_ok")
    if os.path.isfile(sentinel) and os.path.getmtime(sentinel) >= os.path.getmtime(req):
        log("python deps already installed (delete .venv/.deps_ok to force).")
        return
    log("installing python deps (requirements.txt) ...")
    subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip", "--quiet"], check=False)
    subprocess.run([py, "-m", "pip", "install", "-r", req, "--quiet"], check=True)
    try:
        open(sentinel, "w").close()
    except OSError:
        pass


def build_ui() -> None:
    dist = os.path.join(REPO, "webui", "dist")
    if os.path.isdir(dist) and os.path.isfile(os.path.join(dist, "index.html")):
        log("web UI already built (webui/dist).")
        return
    npm = shutil.which("npm")
    if not npm:
        log("npm not found — skipping UI build; the server will serve the fallback dashboard.")
        return
    webui = os.path.join(REPO, "webui")
    log("building the web UI (npm install && npm run build) ...")
    npm_cmd = "npm.cmd" if IS_WIN else "npm"
    try:
        subprocess.run([npm_cmd, "install"], cwd=webui, check=True, shell=IS_WIN)
        subprocess.run([npm_cmd, "run", "build"], cwd=webui, check=True, shell=IS_WIN)
    except Exception as e:  # noqa: BLE001
        log(f"UI build failed ({e}); the server will serve the fallback dashboard.")


def build_eboproto() -> None:
    """Best-effort: build libeboproto so the ctypes protocol path is available. Optional — the pure-Python
    frames.py path works without it. Skipped silently if there's no make/compiler."""
    d = os.path.join(REPO, "autobot", "robot", "native", "eboproto")
    if not os.path.isdir(d):
        return
    if not (shutil.which("make") and (shutil.which("cc") or shutil.which("gcc") or shutil.which("clang"))):
        return
    try:
        subprocess.run(["make", "shared"], cwd=d, check=False, capture_output=True, timeout=120)
        log("built libeboproto (ctypes protocol path enabled).")
    except Exception:  # noqa: BLE001
        pass


def open_browser_later(port: int) -> None:
    def _open():
        time.sleep(2.5)
        try:
            webbrowser.open(f"http://localhost:{port}")
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_open, daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser(description="FreeBo launcher")
    ap.add_argument("--no-venv", action="store_true", help="use the current Python, don't create .venv")
    ap.add_argument("--no-ui-build", action="store_true", help="don't build the web UI")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    ap.add_argument("--port", type=int, default=int(os.environ.get("AUTOBOT_PORT", "8200")))
    args = ap.parse_args()

    os.chdir(REPO)
    py = sys.executable if args.no_venv else ensure_venv()
    try:
        install_deps(py)
    except subprocess.CalledProcessError as e:
        log(f"dependency install failed: {e}")
        return 1
    if not args.no_ui_build:
        build_ui()
    build_eboproto()

    os.environ.setdefault("AUTOBOT_PORT", str(args.port))
    if not args.no_browser:
        open_browser_later(args.port)

    log(f"starting FreeBo on http://localhost:{args.port}  (Ctrl-C to stop)")
    try:
        return subprocess.run([py, "-m", "autobot"], cwd=REPO).returncode
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
