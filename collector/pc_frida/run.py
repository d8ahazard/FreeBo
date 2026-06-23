#!/usr/bin/env python3
"""PC path (fallback): attach Frida to the ROLA app on a USB phone or Android emulator and capture creds.

Uses the SAME hook as the phone path (collector/hooks/agent.js). The hook POSTs directly to the receiver,
and we also forward Frida send() messages to stdout for visibility. Requires the `frida` Python package and
a frida-server running on the target (rooted phone or emulator). See README.md and ../docs/COLLECTOR.md.

Usage:
    python run.py --package com.example.rola --host 127.0.0.1 [--port 8400] [--spawn]
Find the package name with: `frida-ps -Uai`.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
HOOK = REPO_ROOT / "collector" / "hooks" / "agent.js"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", required=True, help="ROLA app package id (see `frida-ps -Uai`)")
    ap.add_argument("--host", default="127.0.0.1", help="receiver host the hook should POST to")
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--spawn", action="store_true", help="spawn the app instead of attaching to a running one")
    args = ap.parse_args()

    try:
        import frida
    except ImportError:
        print("frida not installed. Run: pip install frida frida-tools")
        return 1

    script_src = HOOK.read_text(encoding="utf-8").replace("__AUTOBOT_HOST__", args.host).replace(
        "__AUTOBOT_PORT__", str(args.port))

    device = frida.get_usb_device(timeout=10)
    if args.spawn:
        pid = device.spawn([args.package])
        session = device.attach(pid)
    else:
        session = device.attach(args.package)

    script = session.create_script(script_src)

    def on_message(message, data):
        if message.get("type") == "send":
            payload = message.get("payload", {})
            if isinstance(payload, dict) and payload.get("autobot"):
                v = str(payload.get("value", ""))
                mv = "***" if len(v) <= 6 else f"{v[:2]}…{v[-2:]}"
                print(f"[frida] {payload.get('field')} = {mv}")
        elif message.get("type") == "error":
            print(f"[frida] ERROR: {message.get('description')}")

    script.on("message", on_message)
    script.load()
    if args.spawn:
        device.resume(pid)

    print(f"[run] hooks loaded. Receiver target {args.host}:{args.port}.")
    print("[run] Make sure `python collector/receiver.py` is running, then connect to your robot in the app.")
    print("[run] Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
