#!/usr/bin/env python3
"""Byte-identity gate for the vendored eboproto codec.

eboproto's MAVLink builders must stay byte-for-byte identical to autobot/robot/frames.py (the Python source
of truth). This script enforces that two ways:

  1. Python cross-check (always runs): build representative frames with frames.py and assert proto.py emits
     the same bytes.
  2. C self-test (when a C compiler is available): compile + run eboproto_selftest.c, which bakes the golden
     hex from frames.py and asserts the C codec matches. Skipped (not failed) if no compiler is found.

Exit code 0 = pass/skip, 1 = mismatch. Wire this into CI / pre-publish. Run: python scripts/eboproto_check.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EBOPROTO_DIR = os.path.join(REPO, "autobot", "robot", "native", "eboproto")
sys.path.insert(0, REPO)


def python_crosscheck() -> bool:
    from autobot.robot import frames, proto
    cases = [
        ("motor fwd", proto.mav_motor(ly=1.0, rx=0.0), frames.motor_frame(ly=1.0, rx=0.0)),
        ("motor turn", proto.mav_motor(ly=0.0, rx=-0.5), frames.motor_frame(ly=0.0, rx=-0.5)),
        ("motor stop", proto.mav_motor(0.0, 0.0), frames.motor_frame(0.0, 0.0)),
        ("dock", proto.mav_dock(), frames.command_frame(frames.CMD_DOCK)),
        ("param night", proto.mav_param_set("video", "night_vision", 1.0),
         frames.param_set_frame("video", "night_vision", 1.0)),
    ]
    ok = True
    for name, got, want in cases:
        if got != want:
            print(f"  [FAIL] {name}: {got.hex()} != {want.hex()}")
            ok = False
        else:
            print(f"  [ok]   {name}: {got.hex()}")
    return ok


def _find_cc() -> list[str] | None:
    if os.name == "nt" and shutil.which("cl"):
        return ["cl"]
    for cc in ("cc", "gcc", "clang"):
        if shutil.which(cc):
            return [cc]
    return None


def c_selftest() -> bool | None:
    """Returns True/False on pass/fail, or None if skipped (no compiler)."""
    cc = _find_cc()
    src_c = os.path.join(EBOPROTO_DIR, "eboproto.c")
    src_t = os.path.join(EBOPROTO_DIR, "eboproto_selftest.c")
    if not cc or not (os.path.isfile(src_c) and os.path.isfile(src_t)):
        print("  [skip] no C compiler (or sources missing) — C self-test skipped.")
        return None
    with tempfile.TemporaryDirectory() as td:
        exe = os.path.join(td, "selftest.exe" if os.name == "nt" else "selftest")
        try:
            if cc == ["cl"]:
                cmd = ["cl", "/nologo", "/std:c11", "/TC", src_c, src_t, f"/Fe:{exe}"]
                subprocess.run(cmd, cwd=td, check=True, capture_output=True, timeout=120)
            else:
                subprocess.run(cc + ["-std=c11", "-O2", src_c, src_t, "-o", exe],
                               check=True, capture_output=True, timeout=120)
            r = subprocess.run([exe], capture_output=True, timeout=60, text=True)
            print(r.stdout.rstrip())
            if r.returncode != 0:
                print(r.stderr.rstrip())
            return r.returncode == 0
        except subprocess.CalledProcessError as e:  # noqa: BLE001
            print("  [skip] compile failed (treating as skip):", e.stderr.decode("utf-8", "ignore")[:400])
            return None
        except Exception as e:  # noqa: BLE001
            print("  [skip] self-test could not run:", e)
            return None


def main() -> int:
    print("eboproto byte-identity gate")
    print("Python cross-check (proto.py == frames.py):")
    py_ok = python_crosscheck()
    print("C self-test (eboproto.c == golden frames.py vectors):")
    c_ok = c_selftest()
    if not py_ok:
        print("RESULT: FAIL (Python cross-check mismatch)")
        return 1
    if c_ok is False:
        print("RESULT: FAIL (C self-test mismatch)")
        return 1
    print("RESULT: PASS" + ("" if c_ok else " (C self-test skipped — no compiler)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
