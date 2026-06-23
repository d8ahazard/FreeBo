# AGENTS.md — Autobot

This file is the contract for any agent (human or AI) working on Autobot. Read it before changing code.
The deep detail lives in [`docs/`](docs/) and [`.cursor/rules/`](.cursor/rules/); this is the map.

## What Autobot is

A self-hosted system that gives a configurable AI autonomous control of an Enabot EBO SE robot over the
LAN, with a web UI that shows the robot's live state and the AI's reasoning. See [README.md](README.md).

## One app, clear internal layers — never blur them

Autobot is a **single deployable app** (the `autobot/` package) that runs on one ARM Linux box (a Pi). It
has clear internal layers; keep their boundaries even though there's no network hop between them anymore.

1. **`autobot/robot/`** is the only code that touches the robot. `RobotLink` (link.py) is the in-process
   contract; `NativeRobotLink` drives the real robot via the native TUTK bridge (it owns the only session
   to the robot and reads the secrets); `MockRobotLink` fakes it for hardware-free dev. **Nothing outside
   `autobot/robot/` may talk to the robot directly.** See [docs/BRIDGE_PROTOCOL.md](docs/BRIDGE_PROTOCOL.md).
2. **`autobot/brain/`** is the agent loop, the closed tool set, and the **safety floor**. It is the only
   thing that calls the AI provider, and it reaches the robot only through a `RobotLink`.
3. **`autobot/web/`** serves the UI and the REST + WebSocket API and proxies video; `webui/` is the thin
   React client over it.
4. **`collector/`** is a one-time tool to capture robot credentials. It writes only the top-level `.env` +
   `vendor/`.

The only sub-processes are the unavoidable native ones the link manages: the TUTK `ebo_bridge` binary
(under the bionic linker), `ffmpeg`, and `mediamtx`.

## Hard rules (these are also enforced by `.cursor/rules/`)

- **Safety first.** Every robot-affecting action goes through `autobot/brain/safety.py`. Never let the AI
  bypass speed clamps, the deadman watchdog, the rate limiter, the autonomy mode, or the talk toggle. The
  native link keeps its own deadman stop as a second line of defense. See [docs/SAFETY.md](docs/SAFETY.md).
- **Secrets are isolated, even though everything runs on one box.** Robot credentials
  (`EBO_LICENSE/UID/AUTHKEY/IDENTITY/TOKEN`, `ioctl9930.bin`) load only in `autobot/credentials.py` and are
  consumed only by `NativeRobotLink`. Never log them (mask), never send them to the browser/UI, never send
  them to the AI provider, never commit them. `vendor/` and `.env` are gitignored. See
  [.cursor/rules/40-secrets-and-collector.mdc](.cursor/rules/40-secrets-and-collector.mdc).
- **One source of truth for the protocol.** The MAVLink frame builders + param/eye maps live in
  `autobot/robot/frames.py`; the IOCTL numbers live in `autobot/robot/native/ebo_bridge.c`. Document any
  new frame in [docs/BRIDGE_PROTOCOL.md](docs/BRIDGE_PROTOCOL.md) in the same change.
- **Provider-agnostic AI.** The brain only assumes an OpenAI-compatible Chat Completions endpoint with
  tool/function calling and image input. No hard dependency on any single vendor.
- **The AI's tools are a curated set, composed from skills.** Capabilities live as `Skill`s under
  `autobot/brain/skills/` (core body controls, memory, recognition, voice, home_assistant). The
  `SkillRegistry` aggregates their tools; every call still routes through the safety floor + the owner
  authority gate. Adding a capability = add/extend a skill (with each tool's authority) + a safety rule +
  a doc line in [docs/AI_BRAIN.md](docs/AI_BRAIN.md). Don't scatter ad-hoc robot calls.
- **Persona, memory, and identity are first-class.** The robot has a name/persona (`config.py`), persistent
  memory (`autobot/brain/memory.py`), and an owner-authority/identity layer (`autobot/brain/identity.py`,
  enforced alongside safety). Heavy/optional skills (STT voice, face recognition) must degrade gracefully
  if their dependency or hardware is absent — the app always runs without them.
- **Graceful degradation.** Every subsystem (audio, video, talk, eyes, AI) must fail soft: if it's missing
  or errors, log it and keep the rest working. This is the GrowBot bringup philosophy, kept on purpose.

## Where things live (and where to add things)

- New robot capability (e.g. a new toggle) → frame builder + map in `autobot/robot/frames.py`, handling in
  `NativeRobotLink` (`autobot/robot/native_link.py`, `_do_action`), then an AI tool in
  `autobot/brain/tools.py`, then a doc line. Mirror it in `MockRobotLink` so dev stays hardware-free.
- New AI capability/skill → a module in `autobot/brain/skills/` registered in `build_default_skills()`;
  declare each tool's authority (`anyone`/`owner`). New AI loop behavior → `autobot/brain/agent.py`.
- Movement → the brain emits high-level INTENTS only; the cerebellum (`autobot/brain/locomotion.py`) executes
  them with camera feedback, then `safety.py` clamps. The physical motion model (deadbands, no IMU/ToF on Air
  2) is hard-coded in `autobot/brain/motion_model.py`; see [docs/MOTION.md](docs/MOTION.md). Never send raw
  `ly/rx` from the brain.
- New UI surface → a component in `webui/src/components/`, wired through `webui/src/api.ts`.
- New deployment/image change → `deploy/pi-gen/` (pi-gen config + `stage-autobot`); see [docs/DEPLOY.md](docs/DEPLOY.md).
- The brain uses TWO models: `ai_model` (fast, every tick) + `ai_summarizer_model` (heavy, once-a-day memory
  cleanup in `autobot/brain/summarizer.py`). First-run onboarding is the setup wizard (`/api/setup*`).
- New sensor/telemetry field → `RobotLink.telemetry()` (both impls) + `Observation` in
  `autobot/brain/perception.py` + UI panel.

## Conventions

See [.cursor/rules/50-coding-conventions.mdc](.cursor/rules/50-coding-conventions.mdc). In short: Python is
typed and `ruff`-clean; comments explain *why*, not *what*; `autobot/robot/` stays dependency-light (it runs
on a Pi); the UI is TypeScript + Tailwind; no secrets in logs.

## Provenance

Autobot is built on two upstream projects. The exact borrow/modify/reuse map is in
[docs/PROVENANCE.md](docs/PROVENANCE.md). Respect their licenses (ebo-se-lan-bridge: MIT; GrowBot: CC BY-NC 4.0,
used as reference only).
