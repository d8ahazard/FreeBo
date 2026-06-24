#!/usr/bin/env python3
"""Phase 0.3 capture — window-scoped Air 2 listening diagnostic (Correction 4).

Each window is a SEPARATE measurement epoch: it POSTs /api/diag/audio/reset, waits, then GETs
/api/diag/audio/window (RMS distribution count/min/mean/p50/p90/p95/p99/max, noise floor, enter/exit
thresholds, VAD starts/ends, accepted/dropped segments, STT + queue-wait distributions, transcripts). It
records only what the live AudioSink reports — nothing is fabricated. Use it on the deployment box with the
robot connected; speak as prompted, then transfer the distributions into docs/AUDIO_DIAGNOSTIC.md and pick the
final adaptive-VAD constants from idle-vs-speech RMS.

    python scripts/audio_diag.py --app http://127.0.0.1:8200
    python scripts/audio_diag.py --label "normal_1m" --secs 20      # one custom window
"""
from __future__ import annotations

import argparse
import json
import os
import time

import httpx

# (label, seconds, operator prompt) — the controlled calibration set.
DEFAULT_WINDOWS = [
    ("silence", 30, "Stay SILENT (room ambience only)."),
    ("normal_1m", 20, "Speak NORMALLY at ~1 meter: 'FreeBo stop', 'turn left', 'what do you see'."),
    ("quiet", 20, "Speak QUIETLY."),
    ("loud", 20, "Speak LOUDLY."),
    ("room_noise", 20, "Speak normally WITH typical room noise (TV/fan/etc)."),
    ("tts_playback", 20, "Trigger TTS playback; do NOT speak (measures self-echo into the mic)."),
]


def _window(app: str, label: str, secs: float, prompt: str) -> dict:
    print(f"\n=== {label}: {secs:.0f}s ===\n>>> {prompt}")
    try:
        httpx.post(app + "/api/diag/audio/reset", timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"  reset failed: {e}")
    time.sleep(secs)
    try:
        win = httpx.get(app + "/api/diag/audio/window", timeout=5).json().get("window", {})
    except Exception as e:  # noqa: BLE001
        win = {"error": str(e)}
    win["label"] = label
    print(json.dumps(win, indent=2))
    return win


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0.3 window-scoped audio diagnostic")
    ap.add_argument("--app", default="http://127.0.0.1:8200")
    ap.add_argument("--label", default=None, help="run a single custom window with this label")
    ap.add_argument("--secs", type=float, default=20.0)
    ap.add_argument("--out", default="data/test-evidence/audio_diag.json")
    args = ap.parse_args()

    windows = ([(args.label, args.secs, f"custom window '{args.label}'")]
               if args.label else DEFAULT_WINDOWS)
    report = {"app": args.app, "ts": time.time(),
              "windows": [_window(args.app, lbl, secs, prompt) for (lbl, secs, prompt) in windows]}
    try:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"\nSaved {args.out}. Transfer the idle vs speech RMS distributions into docs/AUDIO_DIAGNOSTIC.md "
              "and set AUTOBOT_STT_RMS_MIN/MAX + ENTER_K/EXIT_K from them.")
    except Exception as e:  # noqa: BLE001
        print(f"(could not save: {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
