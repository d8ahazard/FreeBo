#!/usr/bin/env python3
"""Phone path: prepare a patched ROLA APK that captures your robot credentials.

Two things happen here:
  1. Extract the four TUTK .so libraries from the APK into vendor/lib/ (always — this part is just
     unzip and is reliable).
  2. Inject Frida Gadget + our capture hook (collector/hooks/agent.js, with your PC's host/port baked in)
     so that when you open the patched app on your phone and connect to the robot, the secrets are POSTed
     to the receiver on your PC. The injection is delegated to `objection` (the standard, well-tested tool)
     when available; otherwise we print exact manual steps.

Usage:
    python patch.py --apk /path/to/rola.apk --host <YOUR-PC-LAN-IP> [--port 8400]

Requirements for step 2 (auto-inject): `objection` (pip install objection), plus Android build-tools
(`apksigner`, `zipalign`) and `apktool` on PATH. See README.md. Step 1 needs nothing but Python.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
HOOK = REPO_ROOT / "collector" / "hooks" / "agent.js"
VENDOR_LIB = REPO_ROOT / "vendor" / "lib"
BUILD = HERE / "build"

TUTK_LIBS = ["libTUTKGlobalAPIs.so", "libIOTCAPIs.so", "libRDTAPIs.so", "libAVAPIs.so"]
ABIS = ["armeabi-v7a", "armeabi", "armeabi-v8a", "arm64-v8a"]


def extract_libs(apk: Path) -> int:
    VENDOR_LIB.mkdir(parents=True, exist_ok=True)
    found = 0
    with zipfile.ZipFile(apk) as z:
        names = z.namelist()
        # Prefer armeabi-v7a (the native bridge runs 32-bit). Fall back to other ABIs if needed.
        for abi in ABIS:
            for lib in TUTK_LIBS:
                member = f"lib/{abi}/{lib}"
                if member in names:
                    data = z.read(member)
                    (VENDOR_LIB / lib).write_bytes(data)
        for lib in TUTK_LIBS:
            if (VENDOR_LIB / lib).exists():
                found += 1
    print(f"[patch] extracted {found}/{len(TUTK_LIBS)} TUTK libs -> {VENDOR_LIB}")
    if found < len(TUTK_LIBS):
        print("[patch] WARNING: not all libs found. Check the APK has lib/armeabi-v7a/ TUTK .so files.")
    return found


def render_hook(host: str, port: int) -> Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    js = HOOK.read_text(encoding="utf-8").replace("__AUTOBOT_HOST__", host).replace("__AUTOBOT_PORT__", str(port))
    out = BUILD / "agent.rendered.js"
    out.write_text(js, encoding="utf-8")
    print(f"[patch] hook rendered with receiver {host}:{port} -> {out}")
    return out


def inject_with_objection(apk: Path, hook_js: Path) -> bool:
    if not shutil.which("objection"):
        return False
    out = BUILD / "rola-autobot.apk"
    # objection bakes a Frida gadget that autoloads our script and re-signs the APK.
    cmd = ["objection", "patchapk", "--source", str(apk), "--gadget-version", "latest",
           "--script-source", str(hook_js), "--output", str(out)]
    print("[patch] running:", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
        print(f"[patch] OK: patched APK -> {out}")
        print("[patch] sideload it on your phone, open it, and connect to your robot once.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[patch] objection failed ({e}). See manual steps below.")
        return False


def manual_steps(apk: Path, hook_js: Path):
    print("\n[patch] --- Manual injection (objection not available or failed) ---")
    print("Recommended: install objection and re-run:  pip install objection")
    print("Or do it by hand:")
    print(f"  1. Download Frida Gadget for armeabi-v7a (frida-gadget-<ver>-android-arm.so).")
    print(f"  2. apktool d \"{apk}\" -o build/rola.src")
    print(f"  3. Copy the gadget to build/rola.src/lib/armeabi-v7a/libgadget.so and add a")
    print(f"     libgadget.config.so pointing at this script: {hook_js}")
    print(f"  4. Add `System.loadLibrary(\"gadget\")` to the app's Application.onCreate (smali).")
    print(f"  5. apktool b build/rola.src -o build/rola-autobot.apk")
    print(f"  6. zipalign + apksigner sign with a debug key, then sideload.")
    print("Either way, start `python collector/receiver.py` first, then open the app and connect to the robot.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", required=True, type=Path, help="path to your ROLA .apk")
    ap.add_argument("--host", required=True, help="your PC's LAN IP (where the receiver runs)")
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--libs-only", action="store_true", help="only extract TUTK libs, skip APK injection")
    args = ap.parse_args()

    if not args.apk.exists():
        print(f"[patch] APK not found: {args.apk}")
        return 1

    extract_libs(args.apk)
    if args.libs_only:
        return 0

    hook_js = render_hook(args.host, args.port)
    if not inject_with_objection(args.apk, hook_js):
        manual_steps(args.apk, hook_js)
    print("\n[patch] Next: run `python collector/receiver.py --port %d` on this PC, then open the patched"
          " app on your phone and connect to your robot." % args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
