# FreeBo — Current State

Snapshot of what exists right now. No history, no instructions — see ROADMAP.md for what's next and
PHASE0_ACCEPTANCE.md for the gates.

## Commit
- Base: `59b7c66` ("Upgraaayd"), plus the **uncommitted P0-R4** working tree (central safety / live faculty
  control). Run `git status` for the exact dirty set.
- Frontend build provenance (asset name + content sha + source commit + stale flag) is live at `GET /api/state`
  under `build`, and printed at startup.

## Implemented (verified by unit tests; NOT yet hardware-validated)
- **SafetyKernel** (`autobot/brain/safety.py`): single authority for the five faculties via
  `check_think/check_motion/check_drive/check_say/check_listen/check_see` returning a unified
  `FacultyDecision` (+ `capability_snapshot`). Master inhibit + E-STOP latch + control generation.
- **Master STOP / RESUME** (`/api/estop`, `/api/resume`): STOP atomically inhibits all faculties, latches
  motion, bumps generation, drops to manual, parks reasoning, cancels TTS, preempts the executor, and slams a
  link-level zero burst — operator video/telemetry stay alive. RESUME reconciles the sidecar first and stays
  inhibited if the reset is not acknowledged. Voice STOP uses the same master inhibit.
- **Ability toggles act on live organs**: Move governs manual AND AI motion (no bypass; off preempts+stops);
  Speak off cancels/flushes TTS; Hear off stops VAD/STT+barge-in via a kernel permission hook; Think routed
  through the kernel.
- **Generation reconciliation** (`rtm_node.py` + `scripts/rtm_sidecar.js`): drives carry a generation and the
  sidecar rejects stale ones; estop/reset carry the authoritative generation; a (re)started sidecar defaults
  motion-blocked and is re-asserted on connect; pending waiters fail on sidecar exit; a failed initial drive
  does not start the repeat; process-vs-sidecar `control_state` is exposed and a mismatch blocks motion.
- **Truthful motion readiness** (`agent._motion_block_reason`): blocks on master STOP, no telemetry, RTM
  disconnected, no video frame, resting, not calibrated, HOLD, stale telemetry/video, control mismatch.
- **Capability-status surface**: `GET /api/state` + WS `hello` + a `capabilities` WS event (change-triggered
  + ~5s heartbeat). UI shows requested (lit toggle) vs effective (status dot + reason); header shows
  STOP/RESUME.
- **Truthful audio status** (`audio_sink.audio_status`): transport/requested/effective listening, vad/stt,
  echo-gated, barge-in; `try/finally` STT, synchronized reads, OFF vs INHIBITED, no stale transcript.
- **Overseer puppet mode** + locomotion/probe endpoints (from P0-R3): unchanged.

## Open blockers (Phase 0 NOT passed)
- **Hardware not run**: the E-STOP smoke gate (R4.0) and full hardware acceptance (R4.10) have not been
  executed on the live Air 2. Nothing here is hardware-validated.
- **Test suite exit-hang**: the full `pytest` run still hits a cross-test asyncio/socketpair deadlock on exit
  (a hard per-test timeout now bounds it; root cause not yet fixed). Run suites in groups for green results.
- **R4.3 partial**: `See` off stops AI vision frame intake but explicit cancellation of in-flight vision +
  formal operator/safety/AI frame-path separation are not done.
- **Deterministic test matrix** (R4.9) is partially populated (`test_rtm_node.py` added); the full
  STOP/toggle/frontend matrix is outstanding.

## How the UI is served
`autobot/web/server.py` serves `webui/dist/index.html` + `/assets/*`. `webui/dist` is gitignored; build via
`cd webui && npm run build` (bootstrap rebuilds when missing or stale).
