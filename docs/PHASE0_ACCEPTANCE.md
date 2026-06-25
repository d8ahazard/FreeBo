# Phase 0 — Acceptance

Phase 0 has TWO separate gates (do NOT conflate them):
- **Software safety gate: ACCEPTED FOR PHASE 1** (agent_next_3 Gate A) — the software safety architecture is
  accepted and Phase 1 observability is authorized.
- **Physical gate: PENDING — HARDWARE NOT RUN** — the supervised R4.0 smoke + R4.10 acceptance have not been
  executed on the live Air 2. Hardware eligibility = NO. Physical movement is disabled by policy.

Phase 0 is NOT "fully passed": software acceptance for Phase 1 and physical acceptance are distinct. The frozen
Phase 0 software invariant list lives in `docs/ROADMAP.md`.

Raw machine-readable evidence is stored under `data/test-evidence/` (immutable summary tied to the tested
commit). Hardware runs are joint (operator + live Air 2).

## Software gates

### Test suite (canonical, reproducible)
- Canonical full suite (must exit 0 on three consecutive fresh runs):
  `python -X faulthandler -m pytest -q -p no:recording`
  → **162 passed, 3 skipped** in ~62s; observed clean (exit 0, no leaked tasks, no socketpair hang) on three
  consecutive runs. Raw logs: `data/test-evidence/fullsuite_s6_v1..v3.txt` (gitignored).
- Targeted safety/atomicity groups (all green):
  - `pytest -q -p no:recording tests/test_safety.py tests/test_control_arbiter.py tests/test_rtm_node.py`
  - `pytest -q -p no:recording tests/test_sidecar_protocol.py tests/test_adversarial_integration.py`
  - `pytest -q -p no:recording tests/test_estop_endpoint.py tests/test_reason_cancellation.py tests/test_action_executor.py tests/test_hardware_harness.py`
- Per-test timeout wired (`pytest.ini`: `--timeout=60`, faulthandler 600s).
- The previous full-suite teardown leak (a pending `SpeechService._schedule_clear` task) is FIXED (§6,
  `8030cbd`): tracked + cancelled on teardown; `brain.stop_loop` drains it; `RtmNode.stop` bounded-joins.
  Result: PASS (software test gate).

### Frontend deploy (R4.8 / §9)
- `GET /api/state.build` reports asset + sha256 + source commit + stale flag; startup warns if missing/stale.
- Built with `npm ci && npm run build` (tsc clean). Served entry bundle + content sha + source commit recorded
  in `agent_results.md`.
- Result: PASS (software).

## Hardware gates (NOT yet run — joint)

### R4.0 — E-STOP smoke gate (run FIRST, before further architectural change)
5 eye cmds, 5 forward pulses, 5 turns, 5 stops, 10 latched master-STOP trials (holding forward, mid-turn,
mid-executor-move, several drives in flight, during RTM/sidecar interruption). Capture per command: command_id,
sidecar queue ts, `sdk_send_succeeded`, API response, `robot_effect_observed`, camera/telemetry, latency,
final latch, post-STOP motion. Abort + fix on any failure to halt or any delayed motion.

### R4.10 — full hardware acceptance
- Control delivery: ≥20 each eye/forward/turn/stop/master-STOP (SDK send vs physical effect recorded
  separately).
- Master STOP: ≥20 across joystick / executor move / BACK_UP / queued / active TTS / STT / vision / reasoning
  / RTM reconnect → physical halt every time, no post-stop motion, all faculties cease, operator camera +
  telemetry stay, stale-generation never resumes, explicit RESUME required.
- Ability toggles: each live organ demonstrably starts/stops while active.
- Audio: calibration + critical-command + barge-in + false-positive sets.
- Movement, 30-min stale-stream, supervised 50-step course, 1-hour soak.

## Terminology (must stay precise in all evidence)
`queued_to_sidecar` (stdin write ok) ≠ `sdk_send_succeeded` (Agora send ok) ≠ `robot_effect_observed`
(physical/telemetry confirmation). A successful SDK send is NOT a robot acknowledgment.
