# Provenance — what we borrowed, modified, reused

Autobot is built on two upstream projects in this workspace. This is the exact map of what came from where.

## Sources

- **`ebo-se-lan-bridge/`** — MIT-licensed. The EBO LAN bridge. **The primary code source.**
- **`GrowBot/`** — CC BY-NC 4.0 (Art of the Problem). A legged Pi robot with an LLM-brain *concept*.
  **Reference only — no GrowBot code is copied into Autobot.**

## From `ebo-se-lan-bridge` → Autobot `autobot/robot/` (reuse + modify)

In Autobot v2 the bridge and brain were merged into one app; the upstream bridge code now lives inside the
`autobot/robot/` layer (as `NativeRobotLink` + helpers) rather than a standalone Pi process.

| Upstream file | Autobot file | Disposition |
|---------------|--------------|-------------|
| `app/ebo_bridge.c` | `autobot/robot/native/ebo_bridge.c` | **Modified**: add control `kind 2` (outbound audio / talkback) and SPEAKERSTART; keep video/audio/telemetry/RDT logic. |
| `app/ebo_server.py` | `autobot/robot/native_link.py` (+ `frames.py`, `video.py`) | **Refactored**: the FastAPI supervisor became the in-process `NativeRobotLink`. Keeps MAVLink builders, `PARAM_TOGGLES`, deadman watchdog, video pipeline; the REST surface became the `RobotLink` verbs; **adds** snapshot tap, telemetry, talkback, eye animations (`EYE_ANIMATIONS`). |
| `app/ebo_mqtt.py` | `autobot/robot/mqtt.py` | **Reused** ~as-is (optional Home Assistant entities). |
| `app/ebo.html` | `webui/` | **Ported**: its WebRTC/HLS player + joystick logic moved into the React UI. |
| `app/mediamtx.template.yml` | `autobot/robot/native/mediamtx.template.yml` | **Reused** as-is. |
| `app/run.sh` | `run.sh` (repo root) | **Modified**: single-app entrypoint (`python -m autobot`) + branding. |
| `Dockerfile`, `docker-compose.yml` | `Dockerfile`, `docker-compose.yml` (repo root) | **Merged**: one image for the whole app; copy bundled bionic, path/branding updates. |
| `scripts/build_bridge.sh` | `autobot/robot/native/build_bridge.sh` | **Reused**; `probe_eyes.py` (same dir) added for eye-animation discovery. |
| `docs/SETUP.md` | `docs/COLLECTOR.md` | **Basis for** the collector doc (which secrets, where they come from). |

## From `GrowBot` → Autobot (reference / inspiration only)

| Upstream | Influence in Autobot | Disposition |
|----------|----------------------|-------------|
| `README.md` "LLM Integration" section | `autobot/brain/agent.py` loop shape; `docs/AI_BRAIN.md` | **Concept only** — perceive → LLM → act → feedback. No code copied. |
| `setup/growbot/audio.py` (`speak()` via espeak/aplay) | `autobot/brain/tts.py` TTS approach | **Pattern reference** — shell-out TTS, fail soft. Reimplemented for host + G.711. |
| `setup/growbot/*.py` driver style (graceful degradation, "skip if absent") | Coding conventions; brain subsystem error handling | **Philosophy** — adopted as a rule, not code. |
| MuJoCo sim, learned locomotion, servo/IMU/LED/camera drivers | — | **Not used.** EBO is networked differential drive; no balancing, no servos, no on-Pi sensors. |

## Net-new in Autobot (no upstream)

- The entire `autobot/brain/` + `autobot/web/` (agent loop, provider-agnostic LLM client, perception, tools,
  safety floor, TTS, server + WebSocket) and the `RobotLink` seam (`autobot/robot/link.py`, `mock_link.py`).
- The entire `webui/` React dashboard.
- The entire `collector/` (APK patcher, PC Frida runner, shared hook script, receiver, bundled bionic).
- The eye-animation discovery + `set_eyes` capability.
- Talkback PC pipeline (TTS → G.711 → `/say`) and the native `kind 2` path.

## License hygiene

- Autobot's original code: MIT (see `LICENSE`).
- No TUTK/Kalay SDK, ROLA app code, or keys are redistributed; the user supplies those from their own device.
- Bundled bionic is AOSP Apache-2.0 (see `collector/bionic/NOTICE`).
- GrowBot is CC BY-NC 4.0 and is credited here; since only its ideas are used (no code), Autobot's MIT
  license stands, with attribution to Art of the Problem for inspiration.
