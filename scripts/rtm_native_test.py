"""End-to-end native RTM test (NO browser): spawn the Node RTM sidecar via RtmNode, log in, send a visible
eye change + a short drive nudge, and print inbound telemetry. Disconnect the HUD tab first (same rtm uid)."""
import os
import time

import autobot.config  # noqa: F401  (loads .env)
from autobot.robot.ebo_cloud import EboCloud
from autobot.robot.rtm_node import RtmNode


async def provider():
    return await EboCloud().create_session(int(os.environ.get("EBO_ROBOT_ID", "0")))


def main():
    events = []
    node = RtmNode(provider, on_event=lambda ev: events.append(ev))
    node.start()

    # wait for connect
    for _ in range(30):
        if node.connected:
            break
        time.sleep(0.5)
    print("connected:", node.connected, "| err:", node.last_error)
    if not node.connected:
        print("logs:", node.recent_logs()[-8:]); node.stop(); return

    print("-> eyes(love)"); node.eyes("love"); time.sleep(1.5)
    print("-> eyes(curious)"); node.eyes("curious"); time.sleep(1.5)
    print("-> drive forward 1.0s"); node.drive(0.5, 0.0, duration=1.0); time.sleep(2.0)
    print("-> turn right 0.6s"); node.drive(0.0, 0.5, duration=0.6); time.sleep(1.5)
    node.stop()

    # let telemetry arrive
    time.sleep(3.0)
    print("status:", node.status)
    sent = [e for e in events if e.get("ev") == "sent"]
    peers = [e for e in events if e.get("ev") == "peer"]
    print(f"events: sent={len(sent)} peer={len(peers)} states={[e for e in events if e.get('ev')=='state']}")
    if peers:
        print("sample peer:", peers[-1])
    node.stop()
    print("done")


if __name__ == "__main__":
    main()
