# Phase 0 — Acceptance

Phase 0 passes ONLY when the physical evidence passes. Status today: **NOT PASSED** (software gates largely
met + unit-verified; hardware gates not yet run).

Raw machine-readable evidence is stored under `data/test-evidence/` (immutable summary tied to the tested
commit). Hardware runs are joint (operator + live Air 2).

## Software gates

### Test suite
- Command (per-group, green today):
  `python -m pytest tests/test_safety.py tests/test_rtm_node.py tests/test_action_executor.py tests/test_audio_sink.py tests/test_speech.py tests/test_bargein.py tests/test_motion.py tests/test_behavior.py tests/test_skills_core.py -q -p no:recording`
  → 90 passed.
- `tests/test_checks.py` (16) and `tests/test_motion.py` (10) pass individually.
- Hard per-test timeout is wired (`pytest.ini`: `--timeout=60`, faulthandler 600s).
- OPEN: the FULL-suite single invocation still hits a cross-test asyncio/socketpair exit-hang (now bounded by
  the timeout, not yet root-caused). Result: PARTIAL.

### Frontend deploy (R4.8)
- `GET /api/state.build` reports asset + sha256 + source commit + stale flag; startup warns if missing/stale.
- Verified the served bundle contains the new strings (RESUME / STOPPED (inhibited) / MASTER STOP / BLOCKED).
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
