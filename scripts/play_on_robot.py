"""Play a sound straight out of the robot's own speaker over the native Air 2 link.

This is the bare-metal talkback test: connect RTC, enable the robot intercom (RTM 102003), and stream a WAV
into the channel as G.711 A-law on a declared audio SEND codec so the gateway forwards it to the robot.

    python scripts/play_on_robot.py                 # plays a loud, unmistakable test tone
    python scripts/play_on_robot.py path/to/clip.wav # plays your WAV

If you hear it from the ROBOT, talkback works. (Tone first: it removes TTS quality from the equation.)
"""
from __future__ import annotations

import asyncio
import io
import math
import os
import struct
import sys
import wave

# The audio SEND codec is only declared in the RTC join when talkback is enabled — force it on for this test.
os.environ.setdefault("AUTOBOT_AIR2_NATIVE_TALK", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _tone_wav() -> bytes:
    """A 3 s attention pattern (alternating 880/1320 Hz, loud) at 16 kHz mono — obviously synthetic so there's
    zero doubt it came from us when it plays on the robot."""
    sr = 16000
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    frames = bytearray()
    for i in range(sr * 3):
        t = i / sr
        freq = 880 if int(t * 4) % 2 == 0 else 1320
        amp = 0.6 * 32767
        frames += struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * t)))
    w.writeframes(bytes(frames))
    w.close()
    return buf.getvalue()


async def main(wav_path: str | None) -> int:
    from autobot.robot.air2_native_link import Air2NativeLink

    wav = open(wav_path, "rb").read() if wav_path else _tone_wav()
    src = wav_path or "built-in test tone"
    print(f"[play] source = {src} ({len(wav)} bytes)")

    link = Air2NativeLink()
    link.start()          # RTM control (own thread)
    link._ensure_media()  # RTC receiver (we're inside a running loop here)

    print("[play] waiting for the RTC channel to come up + become publish-ready ...")
    ready = False
    for i in range(60):
        await asyncio.sleep(1)
        vc = link.hub.stats().get("video_count", 0)
        can = link.receiver.can_publish()
        print(f"  t={i:2d}s connected={link._connected()} video_frames={vc} rtc_publish_ready={can} "
              f"err={link.receiver.error}")
        if can:
            ready = True
            break
    if not ready:
        print("[play] FAILED: RTC channel never became publish-ready. Is the robot awake/online?")
        await link.receiver.stop()
        return 2

    print("[play] enabling intercom + streaming audio to the robot speaker ...")
    res = await link.publish_speech(wav)
    print(f"[play] publish_speech -> {res}")

    # Let the publish loop drain the queued G.711 frames (50 fps -> ~clip length), plus margin.
    drain = max(6.0, len(wav) / 32000.0 + 4.0)
    print(f"[play] draining for {drain:.1f}s — LISTEN TO THE ROBOT NOW")
    await asyncio.sleep(drain)
    print(f"[play] media_debug = {link.receiver.media_debug()}")
    await link.receiver.stop()
    print("[play] done. Did the sound come out of the robot?")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if path and not os.path.exists(path):
        print(f"no such file: {path}")
        raise SystemExit(1)
    raise SystemExit(asyncio.run(main(path)))
