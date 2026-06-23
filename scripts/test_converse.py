"""Standalone conversation test: you speak to the robot (via its mic) -> Whisper transcribes -> MiniCPM-V-4.6
generates a coherent in-character reply (aware of what it sees) -> TTS renders it to speech and plays it back.
Proves the hear -> think -> speak loop end to end (independent of the robot-speaker RTC publish)."""
from __future__ import annotations

import base64
import datetime
import re
import subprocess
import sys

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def _ascii(s: str) -> str:
    # strip emoji/non-speakable chars for TTS + console safety
    return re.sub(r"[^\x00-\x7F]+", "", s or "").strip()

APP = "http://127.0.0.1:8200"
VLM = "http://127.0.0.1:8360"
LOG = "collector/captured/converse_test.log"
WAV = "collector/captured/reply.wav"


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    httpx.post(APP + "/api/settings", json={"talk_enabled": True}, timeout=10)
    print(f">>> SPEAK TO THE ROBOT NOW — ask it something ({int(secs)}s) <<<", flush=True)
    rec = httpx.get(APP + "/api/diag/record_audio", params={"secs": secs}, timeout=120).json()
    heard = (rec.get("transcript") or "").strip()
    print(f"  you said: {heard!r}", flush=True)
    if not heard:
        print("  (nothing transcribed — speak louder/closer and retry)")
        return 1

    s = httpx.get(APP + "/api/snapshot.jpg", timeout=20)
    frame = base64.b64encode(s.content).decode() if s.status_code == 200 else None

    print("  thinking…", flush=True)
    r = httpx.post(VLM + "/vlm/decide",
                   json={"frames_b64": [frame] if frame else [], "heard": heard}, timeout=120).json()
    reply = (r.get("text") or "").strip()
    print(f"  robot replies: {reply!r}", flush=True)
    speak = _ascii(reply)

    played = False
    if speak:
        w = httpx.get(APP + "/api/voice/say", params={"text": speak}, timeout=60)
        if w.status_code == 200 and w.content:
            with open(WAV, "wb") as f:
                f.write(w.content)
            print(f"  TTS wav: {len(w.content)//1024} KB -> {WAV} (playing…)", flush=True)
            try:
                subprocess.run(["powershell", "-NoProfile", "-c",
                                f"(New-Object Media.SoundPlayer '{WAV}').PlaySync()"], timeout=30)
                played = True
            except Exception as e:  # noqa: BLE001
                print("  (playback failed:", e, ")")
        else:
            print("  TTS failed:", w.status_code, w.headers.get("X-Reason"))

    block = (f"\n===== CONVERSATION TEST {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====\n"
             f"YOU SAID:    {heard!r}\nROBOT REPLY: {reply!r}\nSPOKEN(WAV): {played}\n" + "=" * 60 + "\n")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(block)
    print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
