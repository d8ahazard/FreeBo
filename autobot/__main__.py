"""Single entrypoint for the unified Autobot app.

    python -m autobot

In `native` mode (the Pi) this supervises mediamtx + ffmpeg + the native TUTK bridge and then serves the
web UI + agent loop. In `mock` mode (set AUTOBOT_ROBOT_LINK=mock) it skips the native children, so the
whole brain + UI + safety floor run on any PC with no robot.
"""
from .web.server import main

if __name__ == "__main__":
    main()
