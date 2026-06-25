# FreeBo

Give a configurable AI **autonomous control** of an **Enabot EBO** robot, with a web UI that shows what the
robot is *thinking* and *doing* in real time — and a hardware-grade safety floor underneath it all.
Self-hosted, local-first, no subscription. Bring your own robot and your own AI model.

FreeBo can:

- **See** — read the live camera feed and feed frames to a vision model (the EBO is camera-only — there is no
  exposed depth/ToF, so navigation is vision-based).
- **Hear** — listen on the robot's mic and transcribe speech (local `faster-whisper`).
- **Move** — drive the treads (analog joystick or AI-chosen vectors), clamped by the safety floor.
- **Speak** — talk through the robot's speaker (local Piper neural TTS, off by default, toggleable).
- **Emote** — set the eye expressions / animations.
- **Think** — an LLM/VLM "brain" perceives, decides, and acts in a loop, streaming its reasoning to the UI.

You bring your own AI (any **OpenAI-compatible** endpoint, or a local VLM/omni model) and your own robot.

> Independent community project. **Not** affiliated with Enabot or ThroughTek. See the legal note below and
> **[docs/SAFETY.md](docs/SAFETY.md)** before driving a real robot.

## Supported robots — two very different transports

FreeBo is **one app** (the `autobot/` package). Which robot you have determines how it connects:

| Family | Transport | Where it runs | Link |
|--------|-----------|---------------|------|
| **EBO SE / Air** | **Fully local** — MAVLink over TUTK/Kalay P2P (DTLS-PSK). Needs the 32-bit ARM TUTK `.so`, so this path runs on an **ARM Linux box** (e.g. a Raspberry Pi). | Pi / ARM Linux | `NativeRobotLink` |
| **EBO Air 2 / Max** | **Cloud** — Agora **RTM** (control) + **RTC** (media). No usable local TUTK. The `air2_native` link runs a headless Node Agora-RTM sidecar + a pure-Python `aiortc` receiver, so it needs **no proprietary `.so`** and runs on a normal **x86** box (Linux/Windows) too. | any PC | `Air2NativeLink` |
| **none (dev)** | Fake robot, no hardware. | anywhere | `MockRobotLink` |

```
EBO SE / Air  ── Kalay P2P / DTLS (LAN) ─────────────┐
                                                     ├─►  FreeBo app  ──►  web UI
EBO Air 2     ── Agora RTM (control) + RTC (media) ──┘    agent loop + SafetyKernel
```

Pick the link with `AUTOBOT_ROBOT_LINK` (`native` | `native_x86` | `air2_native` | `mock`).

## Internal layers

| Layer | What it is |
|-------|------------|
| [`autobot/robot/`](autobot/robot/) | The only robot-facing code. `RobotLink` contract + `NativeRobotLink` (SE: TUTK + ffmpeg/mediamtx + deadman), `Air2NativeLink` (Air 2: Node RTM sidecar + aiortc), `MockRobotLink`. |
| [`autobot/brain/`](autobot/brain/) | The agent loop, provider-agnostic LLM client (+ optional local VLM/omni), the curated tool set, persistent memory, and the **SafetyKernel** (central faculty authority). |
| [`autobot/web/`](autobot/web/) + [`webui/`](webui/) | FastAPI REST + WebSocket + video proxy, serving a React + Vite + Tailwind dashboard. |
| [`collector/`](collector/) | One-time wizard to capture robot credentials (TUTK secrets for SE, Agora cloud session for Air 2) into `.env` + `vendor/`. |

## Safety (read this)

This software drives a real motorized robot, so safety is **mechanical, not prompt-trust** — full reference in
[docs/SAFETY.md](docs/SAFETY.md):

- **One authority.** Every robot-affecting action passes through the SafetyKernel ([`autobot/brain/safety.py`](autobot/brain/safety.py)),
  which decides each faculty (Think / Move / Speak / Listen / See) and clamps drive speed + duration.
- **Master STOP / RESUME.** The big red STOP inhibits *all* autonomous faculties, latches motion, bumps a
  control generation (so in-flight/stale drives are dropped), cancels speech, preempts the active action, and
  drops to manual — while keeping operator video + telemetry alive. An explicit **RESUME** (`/api/resume`)
  reconciles the link first and only then lifts the inhibit.
- **Ability toggles** in the UI gate the live organs (turning *Move* off preempts motion; *Hear* off stops
  STT; *Speak* off cancels TTS).
- **Deadman, twice.** The brain only sends motion in short bursts; the link layer also stops the robot if
  drive frames stop arriving.

## Quick start

**No robot needed (any OS):** clone, then run the launcher — it makes a venv, installs deps, builds the UI,
and opens the dashboard in mock mode.

```bash
./start.sh      # Linux / macOS / Pi
./start.ps1     # Windows (PowerShell)
```

Open `http://localhost:8200` and follow the first-run wizard (pick your AI endpoint/key/model, set a goal).

Or run mock mode directly:

```bash
pip install -r requirements.txt
AUTOBOT_ROBOT_LINK=mock python -m autobot      # UI + fake robot on :8200
```

### Real robot

See **[docs/SETUP.md](docs/SETUP.md)** for the full guide.

- **EBO Air 2 (cloud):** capture the robot's Agora cloud credentials with the onboarding/collector flow, set
  `AUTOBOT_ROBOT_LINK=air2_native`, and ensure **Node.js** is installed (it runs the RTM sidecar). Details:
  [docs/AIR2_CLOUD.md](docs/AIR2_CLOUD.md) + [docs/NATIVE_AIR2.md](docs/NATIVE_AIR2.md).
- **EBO SE (LAN, on a Pi):** capture TUTK secrets ([docs/COLLECTOR.md](docs/COLLECTOR.md)), build the native
  bridge, build the UI, then `docker compose up -d --build`. Set `AUTOBOT_ROBOT_LINK=native`.

The brain uses two models: a **fast** one every tick + a **heavy** one for once-a-day memory cleanup. A
**turnkey Raspberry Pi appliance image** (flash, boot, finish a web wizard) is in [docs/DEPLOY.md](docs/DEPLOY.md).

## Documentation

- [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md) — what exists right now (features, open blockers).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) / [docs/ARCHITECTURE_DECISIONS.md](docs/ARCHITECTURE_DECISIONS.md) — system, data flow, key decisions.
- [docs/SAFETY.md](docs/SAFETY.md) — the safety floor, master STOP/RESUME, faculty authority.
- [docs/AI_BRAIN.md](docs/AI_BRAIN.md) — the agent loop, tool/action contract, provider + VLM/omni config.
- [docs/AIR2_CLOUD.md](docs/AIR2_CLOUD.md) / [docs/NATIVE_AIR2.md](docs/NATIVE_AIR2.md) — the Air 2 cloud (Agora RTM/RTC) link.
- [docs/BRIDGE_PROTOCOL.md](docs/BRIDGE_PROTOCOL.md) — the SE wire protocol + control frames.
- [docs/MOTION.md](docs/MOTION.md) / [docs/NAVIGATION.md](docs/NAVIGATION.md) — the measured motion model + vision-based navigation.
- [docs/COLLECTOR.md](docs/COLLECTOR.md) / [docs/REPRO.md](docs/REPRO.md) — credential capture + reproducing on another robot.
- [docs/DEPLOY.md](docs/DEPLOY.md) — the turnkey Raspberry Pi appliance image.
- [docs/ROADMAP.md](docs/ROADMAP.md) — what's next (observability → cognition → personality).
- [docs/PROVENANCE.md](docs/PROVENANCE.md) — what was borrowed/modified from each upstream project.

## Legal & disclaimer

Independent community project. **Not** affiliated with, authorized, or endorsed by Enabot or ThroughTek.
*Enabot*, *EBO*, *ROLA*, *TUTK*, *Kalay*, *Agora* are trademarks of their respective owners, used
nominatively only. No proprietary components are redistributed; you provide your own robot, credentials, and
vendor libraries (from a device you own and are licensed to use). Original FreeBo code is MIT-licensed (see
[LICENSE](LICENSE)). **Use at your own risk** — this software drives a real motorized robot. Read
[docs/SAFETY.md](docs/SAFETY.md).
