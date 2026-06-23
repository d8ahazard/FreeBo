import asyncio
import sys
import time

import httpx

sys.path.insert(0, ".")
from autobot.diagnostics.motion import frame_diff

B = "http://127.0.0.1:8200"
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 230


async def snap(c):
    r = await c.get(B + "/api/snapshot.jpg")
    return r.content if r.status_code == 200 else None


async def main():
    async with httpx.AsyncClient(timeout=20) as c:
        await c.post(B + "/api/settings", json={"autonomy": "auto", "allow_motion": True})
        moves = [(0.6, 0.0, "fwd"), (0.0, 0.6, "turnR"), (-0.5, 0.0, "back"), (0.0, -0.6, "turnL")]
        i = 0
        t0 = time.time()
        stuck_streak = 0
        while time.time() - t0 < DURATION:
            ly, rx, name = moves[i % 4]
            i += 1
            before = await snap(c)
            r = (await c.post(B + "/api/control", json={"kind": "move", "ly": ly, "rx": rx, "duration": 1.5})).json()
            await asyncio.sleep(2.0)
            after = await snap(c)
            fd = frame_diff(before or b"", after or b"")
            moved = fd is not None and fd > 0.05
            stuck_streak = 0 if moved else stuck_streak + 1
            el = int(time.time() - t0)
            fds = "none" if fd is None else str(round(fd, 3))
            tag = "MOVED" if moved else f"STUCK x{stuck_streak}"
            print(f"t={el:3d}s #{i:2d} {name:5s} ok={r.get('ok')} fd={fds} -> {tag}", flush=True)
            if stuck_streak == 3:
                # grab sidecar logs at the moment it goes stuck
                try:
                    d = (await c.get(B + "/api/debug/rtm")).json()
                    print("  rtm_connected=", d.get("rtm_connected"), flush=True)
                    print("  media=", d.get("media", {}).get("video_pkts"), "v /",
                          d.get("media", {}).get("audio_pkts"), "a; hub=", d.get("media", {}).get("hub"), flush=True)
                    for ln in (d.get("rtm_logs") or [])[-12:]:
                        print("   LOG:", ln, flush=True)
                except Exception as e:
                    print("  debug fetch err", e, flush=True)
            await asyncio.sleep(2.5)


asyncio.run(main())
