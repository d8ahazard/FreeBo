# Architecture

Autobot gives a configurable AI autonomous control of an Enabot EBO SE over the LAN. It is a **single
self-hosted app** (the `autobot/` package) that runs on one ARM Linux box (a Raspberry Pi). The robot's
native protocol libraries (TUTK/Kalay) are 32-bit ARM/Android binaries, so the app runs where they run; it
manages them as child processes rather than splitting across two machines.

> Why one box? The TUTK `.so` files only run on ARM/Android. Putting the whole app on the Pi keeps the
> native link, the AI loop, and the UI in one process tree — no second machine, no LAN hop, no fragile x86
> emulation. For development with no robot, run the same app in `mock` mode on any PC.

## Components (internal layers of one app)

```
 ┌────────────────────┐      Kalay P2P / DTLS-PSK     ┌──────────────────────────────────────────────┐
 │   EBO SE robot      │ ◄────────────────────────────►│           Autobot app (one ARM box)            │
 │  192.168.1.42       │                               │                                                │
 │  cam / mic / motors │                               │  autobot/robot/  (the only robot-facing code)  │
 │  speaker / eyes     │                               │   NativeRobotLink                              │
 └────────────────────┘                               │     ├─ ebo_bridge (native TUTK, bionic)        │
                                                       │     │   stdout: video/audio/telemetry          │
                                                       │     │   fd3:    control (kinds 0/1/2)          │
                                                       │     ├─ ffmpeg + mediamtx (RTSP/WebRTC/HLS)      │
                                                       │     └─ deadman watchdog + JPEG snapshot tap    │
                                                       │            │  (RobotLink interface)             │
                                                       │            ▼                                   │
                                                       │  autobot/brain/                                │
                                                       │   perception.py ─► agent.py ─► providers/ (LLM)│
                                                       │   tools.py ─► safety.py (clamp/deadman/gates)  │
                                                       │   tts.py (TTS -> G.711 -> say)                 │
                                                       │            │                                   │
                                                       │  autobot/web/server.py                         │
                                                       │   REST + WebSocket + video proxy, serves UI    │
                                                       │            │                                   │
                                                       │  webui (React): video, telemetry, live AI      │
                                                       │   thoughts, manual override, config panel      │
                                                       └────────────────────────────────────────────────┘
```

The only sub-processes are the unavoidable native ones the link manages: the TUTK `ebo_bridge` binary,
`ffmpeg`, and `mediamtx`. Everything else (perception, the LLM calls, safety, the web server, the agent
loop) runs in the single Python process.

## The RobotLink seam

`autobot/robot/link.py` defines `RobotLink`, the in-process contract the brain calls (telemetry, snapshot,
drive/move/stop, action, say, connection + video upstream metadata). Two implementations:

- `NativeRobotLink` — drives the real robot via the native TUTK bridge (owns the robot secrets).
- `MockRobotLink` — a fake robot for hardware-free dev (`AUTOBOT_ROBOT_LINK=mock`).

`make_link(settings)` picks one. This replaced the old HTTP `BridgeClient`: there is no longer a network
hop between brain and bridge — the brain calls the link directly in-process.

## The agent loop

`perceive → think → act → observe`, on a configurable tick (default 4s in `auto`).

1. **perceive** — `perception.py` calls `RobotLink.telemetry()` + `RobotLink.snapshot()` → an `Observation`
   (battery, awake, what the camera sees, last action result).
2. **think** — `providers/` sends the system prompt + short history + the JPEG (as an image content part)
   + a telemetry summary to the model, with the tool schema from `tools.py`.
3. **act** — each tool call goes `tools.py → safety.py → RobotLink`. Safety clamps/blocks/rate-limits.
4. **observe** — results are recorded and the reasoning + actions are streamed to the UI via WebSocket.

## No neural network for the wheels

The EBO is a two-wheel differential-drive robot with no balancing requirement. Motion is direct velocity
control (`motor_frame(ly, rx)`); there is nothing to learn. See [PROVENANCE.md](PROVENANCE.md).

## Streams

- **Video for humans:** WebRTC (LAN) with HLS fallback (remote), served by mediamtx, proxied same-origin by
  `autobot/web/server.py` and embedded in the UI.
- **Video for the AI:** periodic JPEG stills via `RobotLink.snapshot()` (cheap; we don't send every frame).
- **Audio in:** the robot mic (listen-only G.711), optionally muxed into the stream.
- **Audio out (talkback):** TTS → G.711 → `RobotLink.say_audio()` → native `kind 2` → robot speaker. See
  [BRIDGE_PROTOCOL.md](BRIDGE_PROTOCOL.md).
