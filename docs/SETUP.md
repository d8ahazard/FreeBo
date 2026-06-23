# Setup

End-to-end bring-up. Autobot is one app: you collect credentials once, then run the app on the Pi (native
mode). For development you can run the same app on any PC in mock mode with no robot.

## Prerequisites

- An **Enabot EBO SE** on your LAN (here: `192.168.1.42`) and the **Enabot/ROLA app** + account (you own them).
- A **Raspberry Pi** (Pi 4 recommended) on the same LAN, with Docker. Its kernel must allow 32-bit ARM
  execution (default on Raspberry Pi OS / HA OS).
- For UI dev / building the web bundle: Node 18+. For mock-mode dev: Python 3.10+.
- An **AI endpoint**: an OpenAI-compatible API key, or a local server (Ollama / LM Studio) with a
  vision-capable model.

## Stage 1 — collect credentials (once)

Follow [COLLECTOR.md](COLLECTOR.md). Result: the top-level `.env` filled in and `vendor/` populated
(`lib/` TUTK `.so`, `ioctl9930.bin`; bionic comes bundled). You only do this once per robot.

## Stage 2 — run the app on the Pi (native mode)

```bash
# copy the repo to the Pi
cp .env.example .env            # if the collector didn't already write it; fill it in
cp -r collector/bionic vendor/bionic        # bundle the AOSP runtime (one-time)
bash autobot/robot/native/build_bridge.sh   # build the native binary (needs the Android NDK)
cd webui && npm install && npm run build && cd ..   # build the web UI
docker compose up -d --build
```

The app exposes (on the Pi's LAN IP):

- `:8200` — the web UI + REST/WebSocket API
- `:8554` RTSP, `:8889` WebRTC, `:8888` HLS — video

Open `http://<pi-ip>:8200`:

1. **AI** — paste your `base_url`, `api_key`, `model`. Pick a vision model for full capability.
2. **Behavior** — set `max_speed`, talk on/off, autonomy mode, and a goal.
3. Press **Go**. Watch the AI's thoughts stream while it perceives and acts. STOP is always available.

Close the Enabot app on your phone (the robot allows one session at a time), or use the UI "release" toggle
to share.

## Try it without hardware (mock mode, any PC)

```bash
pip install -r requirements.txt
AUTOBOT_ROBOT_LINK=mock python -m autobot      # serves the UI + a fake robot on :8200
# (Windows PowerShell: $env:AUTOBOT_ROBOT_LINK="mock"; python -m autobot)
```

The mock link serves telemetry and a synthetic camera image and logs every control call, so you can
exercise the whole loop, the UI, and the safety floor — no robot, no secrets.

## Troubleshooting

- **No video in the UI but telemetry shows connected:** the robot may be asleep — press Wake. On the LAN the
  UI uses WebRTC; if you're remote it falls back to HLS (a few seconds of latency).
- **`connected: false`:** the Enabot app probably holds the single session — close it, or use "release".
- **AI does nothing:** check the model supports tools/vision; the UI shows the provider's last error.
- **Robot won't move:** check autonomy isn't `manual`, `max_speed` > 0, and the STOP latch isn't engaged.
- **Talkback silent:** see the Talkback status note in [BRIDGE_PROTOCOL.md](BRIDGE_PROTOCOL.md); it may be
  unavailable on your unit pending the native send-path discovery.
- **Robot won't connect in native mode:** confirm `.env` has all `EBO_*` secrets and `vendor/` is populated;
  the app logs `missing robot credentials: [...]` if any are absent.
