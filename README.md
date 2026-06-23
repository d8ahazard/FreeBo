# FreeBo

Give a configurable AI **full autonomous control** of an **Enabot EBO** robot over your LAN, with a
lovely web UI that shows what the robot is *thinking* and *doing* in real time. Self-hosted, local-first,
no subscription — clone it, run one script, and bring your robot alive.

FreeBo can:

- **See** — read the live camera feed (and grab still frames for the AI's vision model).
- **Hear** — listen to the robot's microphone.
- **Move** — drive the wheels (analog joystick, d-pad, or AI-chosen vectors).
- **Speak** — talk through the robot's speaker (PC text-to-speech, toggleable in the UI).
- **Emote** — control the eye lights / animations.
- **Think** — an LLM "brain" perceives, decides, and acts in a loop, streaming its reasoning to the UI.

You bring your own AI model (any **OpenAI-compatible** endpoint) and your own robot. FreeBo is provider-
agnostic and self-hosted.

> Not affiliated with Enabot or ThroughTek. See [docs/SAFETY.md](docs/SAFETY.md) and the legal note below.

## How it's put together

FreeBo is **one app** (the `autobot/` package) that runs on a single ARM Linux box (a Raspberry Pi). The
robot's native protocol libraries are 32-bit ARM/Android, so the app runs where they run and manages them
as child processes — there's no separate "bridge" machine and no LAN hop between the robot link and the AI.

```
 EBO SE  ──Kalay P2P / DTLS──►  Autobot app (one Raspberry Pi)
 192.168.1.42                   native TUTK link + agent loop + safety floor + React UI
```

| Internal layer | What it is |
|----------------|------------|
| [`autobot/robot/`](autobot/robot/) | The only robot-facing code. `RobotLink` contract + `NativeRobotLink` (native TUTK + ffmpeg/mediamtx + deadman) and `MockRobotLink` (hardware-free dev). |
| [`autobot/brain/`](autobot/brain/) | The AI agent loop, provider-agnostic LLM client, the closed tool set, and the safety floor. |
| [`autobot/web/`](autobot/web/) + [`webui/`](webui/) | FastAPI REST + WebSocket + video proxy, serving a React + Vite + Tailwind dashboard. |
| [`collector/`](collector/) | One-time wizard to capture your robot credentials (patched ROLA APK or PC Frida) into `.env` + `vendor/`. |

## Quick start

**Fastest (any OS, no robot needed yet):** clone, then run the one-command launcher — it sets up a venv,
installs deps, builds the UI, and opens the dashboard. Off a robot box it auto-runs in mock mode.

```bash
# Linux / macOS / Pi
./start.sh
# Windows (PowerShell)
./start.ps1
```

Then open `http://localhost:8200` and follow the first-run wizard.

See **[docs/SETUP.md](docs/SETUP.md)** for the full guide. The short version for a real robot on a Pi:

1. **Collect credentials** (once): run the collector to capture your robot's TUTK secrets. See [docs/COLLECTOR.md](docs/COLLECTOR.md).
2. **Build + run** on the Pi:
   ```bash
   cp .env.example .env                       # fill in your secrets + AI key (collector can write this)
   cp -r collector/bionic vendor/bionic
   bash autobot/robot/native/build_bridge.sh  # build the native binary (needs the Android NDK)
   cd webui && npm install && npm run build && cd ..
   docker compose up -d --build
   ```
   Open `http://<pi-ip>:8200`, paste your AI endpoint/key/model, set a goal, and press Go.

Want to try the UI and agent loop **without any hardware**? Run the same app in mock mode on any PC:

```bash
pip install -r requirements.txt
AUTOBOT_ROBOT_LINK=mock python -m autobot     # serves the UI + a fake robot on :8200
```

**Turnkey appliance image** (flash an SD card, boot, pick wifi from your phone, finish a web wizard): see
[docs/DEPLOY.md](docs/DEPLOY.md) — built with pi-gen (via WSL2 on Windows) under [`deploy/pi-gen/`](deploy/pi-gen/).
On first boot the wizard asks for your AI provider and suggests a **fast** model (interactions) plus a
**heavy** model (once-a-day memory cleanup).

## Reproduce on another robot

This repo is a lean, fully reproducible base (~1 MB, code only). To stand up FreeBo on another EBO — Air 2 in
particular, which uses the **pure-Python** `air2_native` link (no proprietary `.so`) — follow
**[docs/REPRO.md](docs/REPRO.md)**. In short: `pip install -r requirements.txt`, `npm install` in `webui/`,
capture that robot's credentials with the collector (build the patched app via
`scripts/build_collector_apk.sh` or fetch the prebuilt one with `scripts/fetch_release_assets.*`), fill
`.env`, and run. Big binaries (the patched collector APK, optional wheelhouses) live in **GitHub Releases**,
not git; models are fetched per-machine and never committed; secrets are per-robot.

## Documentation

Everything an agent (human or AI) needs to extend Autobot without re-asking is in [`docs/`](docs/) and
[`.cursor/rules/`](.cursor/rules/):

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the full system, data flow, and component contracts.
- [docs/BRIDGE_PROTOCOL.md](docs/BRIDGE_PROTOCOL.md) — the wire protocol and bridge REST API.
- [docs/AI_BRAIN.md](docs/AI_BRAIN.md) — the agent loop, tool/action contract, and provider config.
- [docs/MATURITY.md](docs/MATURITY.md) — the maturity roadmap: hybrid golden path, latency/benchmark harness, durable run-state, voice routing, memory, navigation.
- [docs/SAFETY.md](docs/SAFETY.md) — the safety floor that sits under the AI.
- [docs/COLLECTOR.md](docs/COLLECTOR.md) — credential collection (phone + PC paths).
- [docs/REPRO.md](docs/REPRO.md) — reproduce FreeBo on another robot (what's in git vs releases, step by step).
- [docs/DEPLOY.md](docs/DEPLOY.md) — building + flashing the turnkey Raspberry Pi appliance image.
- [docs/PROVENANCE.md](docs/PROVENANCE.md) — exactly what we borrowed/modified from each upstream project.

## Legal & disclaimer

Independent community project. **Not** affiliated with, authorized, or endorsed by Enabot or ThroughTek.
*Enabot*, *EBO*, *ROLA*, *TUTK*, *Kalay* are trademarks of their respective owners, used nominatively only.
No proprietary components are redistributed; you provide your own robot, credentials, and TUTK libraries
(from a device you own and are licensed to use). Original Autobot code is MIT-licensed (see [LICENSE](LICENSE)).
**Use at your own risk** — this software drives a real motorized robot. Read [docs/SAFETY.md](docs/SAFETY.md).
