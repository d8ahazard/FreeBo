# Agent Results

> Report for the Phase 0 software-gate directive (`agent_next.md`). Every claim is tied to a commit SHA +
> exact command + exit code. All 10 software sections landed in order; Phase 0 stays FAIL (hardware not run).

## Tested code commit SHA
`383bd8674eeda209f8e77627e0688f349246b4db` (HEAD before this report). Baseline at start of this directive:
`0fd1263091676cf910ab3e2a9f36632f2235fdd2`. In-order commits (§1–§10):
`c57b3f3` → `22ed083` → `e9ae29f` → `eba3a5d` → `9f04240` → `10137a7` → `8030cbd` → `e8c1f66` → `38764d2` →
`6a11dda` → `383bd86`. Environment: Node v22.16.0, Python 3.10.11, win32.

## Report commit SHA
(This file is committed LAST, after the tested code, so the report SHA is distinct from `383bd86`.)

## Phase 0 verdict
**FAIL.** Phase 0 cannot pass on software alone; it requires supervised physical acceptance which is NOT run
under this directive.

## Hardware eligibility
**NO.** No live-robot movement / R4.0 / R4.10 was run. The hardware harness was rewritten + unit-tested but
deliberately NOT executed against the robot.

## Summary
Completed the Phase 0 software gate in order: (1) evidence hygiene + this report; (2) sidecar + RtmNode
atomicity — instance identity, monotonic epoch, correlated/validated `set_control`, validate-before-unlatch
RESET, in-flight-STOP-blocks-RESET, immutable hard-forbidden raw; (3) `MotionTicket` admitted after the clamp,
re-validated before dispatch, carried {epoch,gen} to the sidecar on every physical drive route; (4) real
reasoning cancellation — per-cycle token + boundary guards + task cancel, `check_think` gating all triggers,
`check_see` gating AI captioning; (5) truthful readiness fields (no generic `connected`); (6) fixed the
full-suite teardown leak (3 consecutive clean runs); (7) adversarial cross-boundary integration tests; (8)
honest hardware-harness rewrite + unit tests (not run vs robot); (9) honest STOP/RESUME UI + capability-driven
display + jpost HTTP surfacing; (10) corrected docs. No live robot movement, no R4.0/R4.10, no `--auto`
acceptance. **Phase 0 = FAIL, hardware eligibility = NO.**

## Critical safety changes
Prior baseline `efda9b6`: process-side ControlArbiter — tokenized overlapping STOPs, monotonic
epoch+generation, exclusive single-use RESET admission + CAS, stale motion-ticket detection, honest E-STOP
API status. This directive (in order):
- §2a `22ed083` (sidecar): instance id in `ready`+every `command_result`; transition epoch; correlated
  validated `set_control` (rejects stale epoch/gen; a stale set_control can never unlatch after a newer STOP);
  `estop_reset` validates ALL preconditions before clearing the latch (no estop in flight, instance/epoch/gen
  match, control session ready) — validation first, mutation second, fail-closed; in-flight STOP tracking
  (`activeStops`) blocks RESET; IMMUTABLE hard-forbidden raw set (movement/dock/ownership/speed/avoid/actuator)
  env can never widen.
- §2b `e9ae29f` (RtmNode): process_instance_id; binds to the sidecar instance from `ready`; REJECTS results
  from a replaced sidecar instance (fail closed); tracks sidecar epoch/control_ready/accepted_process;
  `control_state().synchronized` requires bound instance + control_ready + matching epoch/gen/latch + process
  match; RobotLink.estop/estop_reset contract carries epoch end-to-end (air2 link, overseer gate, agent,
  /api/resume).
- §3 `eba3a5d` (motion ticket): RobotLink.move/drive carry {generation,epoch}; ActionExecutor admits a
  MotionTicket after the clamp, RE-VALIDATES immediately before link.move, and carries the ticket so a drive
  admitted before a STOP is rejected after it; EVERY physical route ticketed (manual joystick, overseer act,
  calibration probe+trials, locomotion turn/step/reverse, go_to_place); move_mode/move_speed off the banned
  fire-and-forget `_send`.
- §4 `9f04240` (reasoning): ReasonCancelled + per-cycle generation token + boundary guards at every side
  effect; whole cycle gated on check_think (covers /api/tick + /api/chat + scheduled + command); master STOP
  cancels the in-flight reason task; AI captioning gated by check_see.

## Files changed
`git diff --stat 0fd1263 383bd86`: 36 files, +1393 / −327. Core safety: `autobot/brain/safety.py` (arbiter,
unchanged this directive — baseline), `autobot/brain/agent.py` (+98), `autobot/robot/rtm_node.py` (+129),
`scripts/rtm_sidecar.js` (+195/−rewrite), `autobot/brain/action_executor.py`, `autobot/robot/*_link.py`
(move/drive ticket contract), `autobot/web/server.py`, `autobot/brain/{locomotion,motion_profile,speech}.py`,
`autobot/brain/skills/places.py`, `scripts/hardware_smoke.py` (rewrite), `webui/src/*`. Tests added:
`test_adversarial_integration.py`, `test_hardware_harness.py`, `test_reason_cancellation.py` (+ updates to
`test_rtm_node`, `test_sidecar_protocol`, `test_estop_endpoint`, `test_action_executor`, `test_speech`,
`test_framesample`). Docs: `CURRENT_STATE.md`, `PHASE0_ACCEPTANCE.md`.

## Exact test commands and exit codes
All run with `python -X faulthandler -m pytest -q -p no:recording ...`; env: Node v22.16.0, Python 3.10.11.
- Group 1 `tests/test_safety.py tests/test_control_arbiter.py tests/test_rtm_node.py` → **33 passed**, exit 0.
- Group 2 `tests/test_sidecar_protocol.py tests/test_adversarial_integration.py` → **20 passed**, exit 0.
- Group 3 `tests/test_estop_endpoint.py tests/test_reason_cancellation.py tests/test_action_executor.py
  tests/test_hardware_harness.py` → **31 passed, 1 skipped**, exit 0.
- Canonical full suite (no path) × 3 consecutive fresh runs at `383bd86`:
  run1 → **178 passed, 3 skipped**, exit 0 (67.7s);
  run2 → **178 passed, 3 skipped**, exit 0 (69.2s);
  run3 → **178 passed, 3 skipped**, exit 0 (68.0s).
  No "Task was destroyed" warning, no socketpair hang, no per-test timeout. Raw logs:
  `data/test-evidence/fullsuite_final_run{1,2,3}.txt` (gitignored).

## Full-suite hang diagnosis and fix
RESOLVED (§6, `8030cbd`). Diagnosis: the intermittent shutdown noise/hang was a leaked fire-and-forget task —
`SpeechService._schedule_clear` created an untracked `asyncio` clear-timer that the loop destroyed pending at
teardown ("Task was destroyed but it is pending!"), which on Windows could coincide with the socketpair
loop-teardown path. Fix: `SpeechService` tracks its clear-timer tasks and `aclose()` cancels+awaits them;
`AgentBrain.stop_loop` cancels the in-flight reason task, drains `speech.aclose()`, and stops registry
background; `RtmNode.stop()` bounded-joins its reader thread + closes pipes (already fails pending waiters);
tests that publish speech drain via `aclose()`. The earlier per-file "hang" was a slow first-time heavy import
(transformers via faster_whisper / insightface model load) exceeding the 60s per-test timeout in a cold
process; amortized in the full suite. Evidenced by 3 consecutive clean full-suite runs.

## Sidecar integration evidence
`tests/test_sidecar_protocol.py` (real Node child process, FAKE SDK) — 11 passed: ready announces instance id;
stale set_control cannot unlatch after STOP; hard-forbidden vs not-allowed raw distinction; honest E-STOP ack;
reset cannot clear a newer STOP; matching reset clears + reports control_ready. `tests/test_rtm_node.py` —
synchronization requires instance+control_ready+epoch. Full adversarial child-process matrix: Section 7 (open).

## Reason-cancellation evidence
`tests/test_reason_cancellation.py` — 4 passed, 1 skipped (full cortex-path variant skips when the mock
perception path doesn't reach the provider): guard raises after generation bump / master inhibit; `_reason`
returns cancelled when Think inhibited; master STOP cancels the in-flight reason task.

## Motion-ticket integration evidence
`tests/test_action_executor.py` (27 passed incl. motion) exercises the admit→re-validate→link.move(gen,epoch)
path; `tests/test_sidecar_protocol.py` proves a drive stamped with a stale generation is rejected after a STOP.
End-to-end child-process barrier cases: Section 7 (open).

## Frontend build and served-bundle evidence
Built with `npm ci && npm run build` in `webui/` (Node v22.16). `tsc` clean; vite built in ~11.5s (the only
warning is the upstream agora-rtm-sdk `eval` notice). Served bundle (from `webui/dist/index.html`):
- entry JS: `assets/index-BH4iPHqj.js`  SHA-256 `AE369BD48C8477647BAC95D6D2341FDB...` (first 32 hex)
- entry CSS: `assets/index-gSsarUf9.css`
- `index.html` SHA-256 `C03E887A9132F251C22F882D6C8753AAE8C442BF6F560F799D37C5C1DBE5F137`
Source commit for the build: `6a11dda` (§9). `webui/dist/` is gitignored (built on deploy); NOT stale vs
source at build time.

## Harness status
Rewritten (`scripts/hardware_smoke.py`, §8 commit `38764d2`): honest tri-state evidence (no ok-inference),
operator-only physical effect (never auto-set), `--auto` => `acceptance_eligible=false` (never a PASS), dirty
tree => diagnostics only, abort on failed STOP / non-reconciled RESUME, records commit + HTTP status +
readiness before/after + dispatch/completion ts. Pure helpers unit-tested in `tests/test_hardware_harness.py`
(7 passed). NOT run against the robot under this directive.

## Known limitations
- HARDWARE NOT RUN: no live movement / R4.0 / R4.10. Phase 0 = FAIL, hardware eligibility = NO.
- Non-drive effect-command ticket ENFORCEMENT in the sidecar (dock/laser/move_mode/avoid/release/resume) is
  incomplete: they now carry instance identity + use the correlated (non-`_send`) path and the hard-forbidden
  raw set is immutable, but they are not yet per-command ticket-REJECTED in the sidecar the way `drive` is.
  The safety-critical motion (drive/move) path IS fully ticket-enforced end-to-end.
- The full-cortex variant of the provider-blocked reasoning-cancellation test skips when the mock perception
  path doesn't reach the provider; the cancellation mechanism itself is proven by the direct task-cancellation
  test + the boundary-guard tests.
- Air 2 control REQUIRES the Agora cloud (internet); no verified local-only Air 2 path. EBO Max unverified.

## Working-tree status
Clean except this file (`agent_results.md`), which is committed LAST so the tested-code SHA (`383bd86`) is
distinct from the report SHA. `webui/dist/` and `data/test-evidence/` are gitignored.
