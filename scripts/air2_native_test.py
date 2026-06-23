"""Full native Air2 test (NO browser): Air2NativeLink brings up RTC (video) + RTM (control) together from one
session, then drives. The robot only obeys drive while an app participant is in the RTC channel, so this is
the real test. Disconnect the HUD/browser tab first (shared uids)."""
import asyncio
import time

import autobot.config  # noqa: F401
from autobot.robot.air2_native_link import Air2NativeLink


async def main():
    link = Air2NativeLink()
    link.start()
    for _ in range(40):
        if link.rtm.connected:
            break
        await asyncio.sleep(0.5)
    print("RTM connected:", link.rtm.connected, "| logs:", link.rtm.recent_logs()[-3:])

    await link.telemetry()  # kicks off the RTC receiver
    for _ in range(25):
        t = await link.telemetry()
        if t.get("frames_received"):
            break
        await asyncio.sleep(1)
    t = await link.telemetry()
    print("RTC frames:", t.get("video_frames"), "| frames_received:", t.get("frames_received"))

    print("-> eyes(love)"); await link.action("eyes_love"); await asyncio.sleep(1.5)
    print("-> drive FORWARD 1.5s"); await link.move(0.5, 0.0, 1.5); await asyncio.sleep(2.5)
    print("-> turn RIGHT 1.0s"); await link.move(0.0, 0.5, 1.0); await asyncio.sleep(2.0)
    print("-> drive BACK 1.0s"); await link.move(-0.5, 0.0, 1.0); await asyncio.sleep(2.0)
    await link.stop()

    t = await link.telemetry()
    print("telemetry:", {k: t.get(k) for k in ("connected", "battery", "charge", "video_frames", "resting", "tof")})
    link.close()
    time.sleep(1)
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
