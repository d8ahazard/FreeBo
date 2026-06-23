"""End-to-end native media test: cloud session -> AgoraNativeReceiver -> MediaHub -> VSLAM + AudioSink.

Run with NO browser tab connected (the native receiver joins as the sole app participant; a second joiner on
the same uid causes Agora UID_CONFLICT). Prints rolling video/audio/SLAM stats + any transcribed speech.
"""
import asyncio
import os
import time

import autobot.config  # noqa: F401  (loads .env)
from autobot.brain.audio_sink import AudioSink
from autobot.brain.slam import VisualSlam
from autobot.robot.agora_native import AgoraNativeReceiver
from autobot.robot.ebo_cloud import EboCloud
from autobot.robot.media_hub import MediaHub


async def main():
    robot_id = int(os.environ.get("EBO_ROBOT_ID", "0"))
    session = await EboCloud().create_session(robot_id)
    if not session.get("ok"):
        print("session failed:", session); return
    print("session ok: channel=%s uid=%s" % (session["rtc"]["channel"], session["rtc"]["uid"]))

    hub = MediaHub()
    probe = {"n": 0, "bgr": 0, "shape": None}

    def _probe(f):
        probe["n"] += 1
        if f.bgr is not None:
            probe["bgr"] += 1
            probe["shape"] = f.bgr.shape
            if probe.get("cverr") is None and probe["bgr"] == 3:
                try:
                    import cv2
                    g = cv2.cvtColor(f.bgr, cv2.COLOR_BGR2GRAY)
                    ok, buf = cv2.imencode(".jpg", f.bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                    probe["cverr"] = f"OK gray={g.shape} jpeg={ok}/{len(buf) if ok else 0} dtype={f.bgr.dtype} contig={f.bgr.flags['C_CONTIGUOUS']}"
                except Exception as e:
                    probe["cverr"] = f"{type(e).__name__}: {e}"
    hub.subscribe_video(_probe)

    slam = VisualSlam()
    slam.attach(hub)
    print("VSLAM enabled:", slam.enabled)
    sink = AudioSink(on_utterance=lambda t: print("  HEARD:", t))
    sink.attach(hub)

    recv = AgoraNativeReceiver(session, hub)
    recv.start()

    t0 = time.time()
    while time.time() - t0 < 25:
        await asyncio.sleep(3)
        st = hub.stats()
        jpeg = hub.latest_video_jpeg()
        print("t=%2ds conn=%s streams=%s video=%d audio=%d jpeg=%s slam=%s err=%s" % (
            int(time.time() - t0), recv.connected, recv._streams, st["video_count"], st["audio_count"],
            (len(jpeg) if jpeg else 0), slam.pose(), recv.error))
    await recv.stop()
    slam.stop(); sink.stop()
    print("FINAL:", hub.stats(), "slam:", slam.pose())
    print("PROBE:", probe, "slam_err:", slam.last_error)


if __name__ == "__main__":
    asyncio.run(main())
