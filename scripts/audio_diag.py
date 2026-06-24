#!/usr/bin/env python3
"""Phase 0.3 operator helper — capture the live Air 2 listening diagnostic.

This drives the read-only `/api/diag/audio` + `/api/diag/heard` endpoints on a RUNNING app to capture an idle
(silence) window and a speech window, then prints a findings snapshot to paste into docs/AUDIO_DIAGNOSTIC.md.
It does NOT fabricate anything — it just records what the live AudioSink reports, so the adaptive-VAD constants
are traceable to a real run. Requires the app + a real robot mic streaming.

    python scripts/audio_diag.py --app http://127.0.0.1:8200 --idle 10 --speech 15

During the SPEECH window, say a few short commands ("FreeBo, stop", "turn left", "what do you see").
"""
from __future__ import annotations

import argparse
import json
import time

import httpx


def _snap(app: str) -> dict:
    out = {}
    try:
        out["audio"] = httpx.get(app + "/api/diag/audio", timeout=5).json().get("audio_sink", {})
    except Exception as e:  # noqa: BLE001
        out["audio_error"] = str(e)
    try:
        out["heard"] = httpx.get(app + "/api/diag/heard", timeout=5).json().get("heard", [])
    except Exception as e:  # noqa: BLE001
        out["heard_error"] = str(e)
    return out


def _window(app: str, label: str, secs: float) -> dict:
    print(f"\n=== {label} window: {secs:.0f}s ===")
    if label.startswith("SPEECH"):
        print(">>> SPEAK NOW: short commands, normal volume/distance.")
    start = _snap(app)
    time.sleep(secs)
    end = _snap(app)
    a0, a1 = start.get("audio", {}), end.get("audio", {})
    d = {
        "label": label, "secs": secs,
        "recv_delta": (a1.get("recv", 0) - a0.get("recv", 0)),
        "chunks_delta": (a1.get("chunks", 0) - a0.get("chunks", 0)),
        "max_rms": a1.get("max_rms"), "noise_floor": a1.get("noise_floor"),
        "enter_thr": a1.get("enter_thr"), "exit_thr": a1.get("exit_thr"), "adaptive": a1.get("adaptive"),
        "vad_starts_delta": (a1.get("vad_starts", 0) - a0.get("vad_starts", 0)),
        "vad_ends_delta": (a1.get("vad_ends", 0) - a0.get("vad_ends", 0)),
        "seg_accepted_delta": (a1.get("seg_accepted", 0) - a0.get("seg_accepted", 0)),
        "seg_dropped_delta": (a1.get("seg_dropped_short", 0) - a0.get("seg_dropped_short", 0)),
        "drop_speaking_delta": (a1.get("drop_speaking", 0) - a0.get("drop_speaking", 0)),
        "last_stt_ms": a1.get("last_stt_ms"), "last_queue_wait_ms": a1.get("last_queue_wait_ms"),
        "transcripts": end.get("heard", [])[-8:],
    }
    print(json.dumps(d, indent=2))
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0.3 live audio diagnostic capture")
    ap.add_argument("--app", default="http://127.0.0.1:8200")
    ap.add_argument("--idle", type=float, default=10.0)
    ap.add_argument("--speech", type=float, default=15.0)
    ap.add_argument("--out", default="data/test-evidence/audio_diag.json")
    args = ap.parse_args()

    report = {"app": args.app, "ts": time.time(),
              "idle": _window(args.app, "IDLE (silence)", args.idle),
              "speech": _window(args.app, "SPEECH", args.speech)}
    try:
        import os
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"\nSaved {args.out}. Transfer these numbers into docs/AUDIO_DIAGNOSTIC.md and set the final "
              "adaptive-VAD constants from the observed idle vs speech RMS.")
    except Exception as e:  # noqa: BLE001
        print(f"(could not save: {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
