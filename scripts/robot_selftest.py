#!/usr/bin/env python3
"""Live robot capability self-test.

Drives the RUNNING Autobot app through its HTTP API and reports a clear PASS/FAIL/SKIP for each capability:
connection, video, eyes, move (with motion confirmation), rotate, talk, hear, autonomy, and VSLAM. It never
opens its own robot session — it uses the app's, so run it while `python -m autobot` is up.

Safe by default: motion bursts are short and speed/duration-clamped by the app's safety floor, and the run
always restores your settings and issues an emergency stop at the end.

Examples:
  python scripts/robot_selftest.py                      # full suite (no talk/hear playback), local app
  python scripts/robot_selftest.py --no-move            # read-only-ish: skip the driving checks
  python scripts/robot_selftest.py --talk --hear        # also play audio + ask you to speak
  python scripts/robot_selftest.py --only connection,video,eyes
  python scripts/robot_selftest.py --json > report.json
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# The report uses a few unicode glyphs (✔ ✘ → ─). Windows consoles default to cp1252 and would crash on
# them — force UTF-8 (replace anything still unencodable rather than erroring).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - older/odd stdout objects: best-effort
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autobot.diagnostics.checks import Options          # noqa: E402
from autobot.diagnostics.runner import _make_ask, run_selftest   # noqa: E402


def _csv(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def main() -> int:
    ap = argparse.ArgumentParser(description="FreeBo live robot capability self-test")
    ap.add_argument("--app", default=os.environ.get("AUTOBOT_APP_URL", "http://127.0.0.1:8200"),
                    help="Base URL of the running app (default: http://127.0.0.1:8200)")
    ap.add_argument("--no-move", action="store_true", help="Skip checks that physically drive the robot")
    ap.add_argument("--talk", action="store_true", help="Play a TTS phrase through the robot speaker")
    ap.add_argument("--hear", action="store_true", help="Interactive: ask you to speak and confirm STT")
    ap.add_argument("--speed", type=float, default=0.4, help="Drive magnitude for motion checks (0..1)")
    ap.add_argument("--only", help="Comma-separated check ids to run (e.g. connection,video,move)")
    ap.add_argument("--skip", help="Comma-separated check ids to skip")
    ap.add_argument("--json", action="store_true", help="Also print a JSON report at the end")
    ap.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    args = ap.parse_args()

    opts = Options(
        allow_move=not args.no_move,
        test_talk=args.talk,
        test_hear=args.hear,
        speed=max(0.0, min(1.0, args.speed)),
        on_progress=lambda m: print(m, flush=True),
        ask=_make_ask(args.hear),
    )
    return asyncio.run(run_selftest(
        args.app, opts, only=_csv(args.only), skip=_csv(args.skip),
        json_out=args.json, color=not args.no_color,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
