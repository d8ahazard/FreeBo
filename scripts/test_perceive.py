"""Standalone perception test: grab a live camera frame + a few seconds of robot-mic audio (transcribed),
feed both to the vision/reasoning model (MiniCPM-V-4.6), and log what the robot observes, reasons, and
decides to investigate. No driving — pure see/hear/think. Output is written to a log for review."""
from __future__ import annotations

import base64
import datetime
import sys

import httpx

APP = "http://127.0.0.1:8200"
VLM = "http://127.0.0.1:8360"
LOG = "collector/captured/perceive_test.log"


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    print("Grabbing camera frame…", flush=True)
    s = httpx.get(APP + "/api/snapshot.jpg", timeout=20)
    if s.status_code != 200:
        print("no camera frame:", s.headers.get("X-Reason"))
        return 1
    frame = base64.b64encode(s.content).decode()
    print(f"  frame {len(s.content)//1024} KB", flush=True)

    print(f"Listening to the robot's mic for {int(secs)}s (speak/make noise if you want)…", flush=True)
    try:
        rec = httpx.get(APP + "/api/diag/record_audio", params={"secs": secs}, timeout=90).json()
        heard = rec.get("transcript", "") or ""
        print(f"  heard: {heard!r} (rms={rec.get('rms')})", flush=True)
    except Exception as e:  # noqa: BLE001
        heard = ""
        print("  (audio capture failed:", e, ")", flush=True)

    print("Thinking (MiniCPM-V-4.6)…", flush=True)
    r = httpx.post(VLM + "/vlm/observe", json={"frames_b64": [frame], "heard": heard}, timeout=180).json()
    thoughts = r.get("thoughts", r.get("error", "(no output)"))

    block = (f"\n===== PERCEPTION TEST {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====\n"
             f"HEARD: {heard!r}\n\nROBOT'S THOUGHTS:\n{thoughts}\n" + "=" * 60 + "\n")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(block)
    print(block)
    print(f"(logged to {LOG})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
