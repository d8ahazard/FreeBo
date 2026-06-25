# Agent Next: Finish the Phase 0 Software Gate

This file is the authoritative next-work directive for the coding agent.

## Communication contract

1. Do not answer in chat and do not ask what to do next.
2. Work continuously through the ordered plan below in small, reviewable commits.
3. Communicate progress and final results only through `agent_results.md` in the repository root.
4. Create `agent_results.md` immediately if it does not exist. Update it after each meaningful commit, replacing stale claims rather than appending contradictory history.
5. Every claim in `agent_results.md` must be tied to an exact commit SHA and an exact command with its exit code.
6. Never claim Phase 0 PASS from software tests alone. Phase 0 remains FAIL until supervised physical acceptance passes.
7. Do not run live robot movement, the hardware smoke gate, R4.0, or R4.10 under this directive.
8. Do not use `--auto` to generate acceptance evidence. Automatic runs are diagnostics only and must state `acceptance_eligible=false`.

## Current baseline

- Repository head at issuance: `ab599f54fd1e35eaf0e069f3f0652473b59dfc72`.
- Last meaningful implementation commit: `efda9b60facfcd8fcdfe43611de9b439d20675bd`.
- Process-side STOP/RESET arbitration is materially improved:
  - tokenized overlapping STOP dispatches,
  - monotonically increasing epoch and generation,
  - exclusive single-use RESET admission,
  - process-side RESET compare-and-swap,
  - stale motion-ticket detection,
  - honest E-STOP API status.
- The current repository still does **not** have a complete end-to-end atomic safety path.
- The one-process full test suite still intermittently hangs on Windows. Running each test file separately is useful diagnosis, but it is not an acceptance substitute.
- `tc_out.txt` and `tc_err.txt` are scratch evidence and should not be committed at the repository root.
- Hardware eligibility: **NO**.
- Phase 0 status: **FAIL**.

## Required work order

Complete the following in order. Do not skip ahead to hardware or documentation victory laps.

---

## 1. Clean test-evidence hygiene

- Remove `tc_out.txt`, `tc_err.txt`, and any other root-level scratch test logs.
- Put human-readable results in `agent_results.md`.
- Put machine-readable immutable evidence under `data/test-evidence/` only when it is tied to:
  - the exact tested commit SHA,
  - a clean working tree,
  - the exact command,
  - start/end timestamps,
  - exit code,
  - platform and Python/Node versions.
- Do not commit a row of dots and call it reproducibility.

The first version of `agent_results.md` must state the current baseline, current blockers, and `Phase 0: FAIL` before implementation continues.

---

## 2. Complete sidecar atomicity, instance identity, and reconciliation

The Python arbiter is not enough. The motion-emitting Node sidecar must enforce the same transition contract.

### 2.1 Process and sidecar identities

Add:

- a `process_instance_id` generated once per Python server process,
- a `sidecar_instance_id` generated at every Node sidecar startup.

The sidecar must include its instance ID in:

- `ready`,
- connection-state events,
- every `command_result`,
- reconciliation state.

Python must reject responses from a replaced or unexpected sidecar instance.

### 2.2 Epoch and generation everywhere

The accepted sidecar control state must include:

- process instance ID,
- sidecar instance ID,
- transition epoch,
- control generation,
- desired/observed latch,
- control-ready state.

Both epoch and generation must be monotonic. Lower values are rejected. Equal values may be accepted only when they do not weaken a newer or currently latched state.

### 2.3 Correlated `set_control`

Replace fire-and-forget `set_control` with a correlated, acknowledged operation.

The acknowledgment must prove that the current sidecar instance accepted the current process instance, epoch, generation, and latch state. `sidecar_control_ready` becomes true only after this acknowledgment validates.

A stale queued `set_control` must never unlatch or lower the generation after a newer STOP.

### 2.4 Sidecar RESET must validate before mutation

The sidecar must not execute `latched = false` until all reset preconditions are validated:

- no E-STOP dispatch is active,
- expected process instance matches,
- expected sidecar instance matches,
- expected epoch equals the accepted epoch,
- expected generation equals the accepted generation,
- RTM is connected,
- the control session is ready,
- the reset attempt is the currently admitted one.

Validation first. Mutation second. Any failure leaves the sidecar latched.

Do not report `sent_to_agora=true` for a local control-state mutation. Use explicit fields such as:

- `control_state_applied`,
- `reconciled`,
- `control_ready`,
- `latched`,
- `epoch`,
- `generation`.

### 2.5 Tokenized E-STOP dispatch in the sidecar

Track active E-STOP dispatch identity, not a single Boolean.

A priority STOP must synchronously:

1. register its dispatch token,
2. latch,
3. adopt only a monotonic epoch and generation,
4. invalidate queued and active effect commands from older tickets,
5. clear drive timers,
6. dispatch and await the initial zero frame,
7. schedule retry zero frames,
8. emit the correlated result,
9. clear only its own active dispatch token.

RESET is rejected while any STOP dispatch remains active.

### 2.6 Invalidate all stale effect commands

The queue currently protects primarily `drive`. Extend the rule to every robot-affecting command:

- drive/move,
- dock,
- dock-and-release/go-home,
- release control,
- resume control,
- movement mode,
- movement speed,
- avoidance changes,
- laser/actuator commands,
- calibration movement,
- any future physical effect.

Every effect command carries a process instance, sidecar instance, epoch, and generation ticket. The sidecar rejects it unless the ticket exactly matches the accepted current control state and motion is permitted.

STOP and explicit zero-motion/deadman commands remain permitted.

### 2.7 Raw RTM must remain safe by construction

- Unknown IDs are denied.
- Add an immutable hard-forbidden set for every known movement, docking, ownership, speed/mode, avoidance, and actuator ID.
- Environment configuration must never override the hard-forbidden set.
- Prefer removing production raw expansion entirely. If retained for diagnostics, require an explicit unsafe-development mode that is disabled during all acceptance runs.

---

## 3. Wire `MotionTicket` into the real dispatch path

The current `ControlArbiter.admit_motion()` and `validate_ticket()` logic is useful but not sufficient until the ticket reaches the final transport boundary.

### Required flow

1. `SafetyFloor.check_drive()` performs policy/clamping.
2. `SafetyFloor.admit_motion()` returns a `MotionTicket` only after the request is allowed.
3. `ActionExecutor` stores that ticket on the action.
4. Immediately before calling the link, `ActionExecutor` validates the ticket again.
5. The typed RobotLink motion call carries epoch and generation.
6. `Air2NativeLink` passes them to `RtmNode`.
7. `RtmNode` checks the current process-side state immediately before writing to stdin.
8. The Node sidecar validates the same ticket again before sending any RTM effect.

A drive authorized before STOP but not yet written must fail after STOP.

Audit every physical route, not merely the AI drive tool:

- manual joystick,
- overseer controls,
- locomotion helpers,
- ActionExecutor recovery,
- calibration movement,
- automatic docking,
- dock/undock/go-home,
- release/resume control,
- movement speed/mode changes.

No physical route may call `RtmNode._send`, `raw`, or an un-ticketed effect method directly.

---

## 4. Make reasoning cancellation and faculty inhibition real

Incrementing `_reason_gen` is not cancellation unless every side-effect boundary checks it.

### Required implementation

- Track the active reason task.
- At the start of every reason cycle, capture a reason-generation token.
- Master STOP invalidates the generation and cancels the active task where supported.
- Add a common `reason_still_valid(token)` or equivalent check.
- Check validity and `check_think()`:
  - after perception,
  - after registry observation hooks,
  - after every VLM/omni/provider await,
  - before emitting thoughts,
  - before appending any history,
  - before every tool call,
  - after every tool result,
  - before speech,
  - before motion,
  - before memory mutation,
  - before scheduling or alerts,
  - before behavior/personality mutation.
- A stale cycle exits with an explicit cancelled result and performs no additional side effects.
- `/api/tick`, `/api/chat`, scheduled reasoning, command-driven reasoning, and direct triggers must all pass through `check_think()`.
- `check_see`, `check_listen`, and `check_speak` must govern the real live organ entry points, not merely UI state.

Required tests:

- STOP while a provider request is blocked,
- STOP after provider completion but before tool execution,
- STOP between two tool calls,
- STOP before history append,
- `/api/tick` and `/api/chat` while inhibited,
- no stale reasoning result resurfaces after RESUME.

---

## 5. Finish truthful readiness and single-authority migration

Expose distinct state, with no generic `connected` shortcut:

- `rtc_video_connected`,
- `rtm_connected`,
- `sidecar_process_ready`,
- `sidecar_control_ready`,
- `process_instance_id`,
- `sidecar_instance_id`,
- `process_latched`,
- `sidecar_latched`,
- `process_epoch`,
- `sidecar_epoch`,
- `process_generation`,
- `sidecar_generation`,
- `synchronized`,
- `stop_in_flight`,
- `reset_active`,
- `last_reconcile_error`.

Motion readiness requires actual RTM control readiness, current instance identity, synchronized epoch/generation/latch, and a valid motion ticket. Recent RTC video must never imply motion readiness.

Finish routing these through the central authority:

- STT and listening,
- TTS and WebSocket call speech,
- AI captioning and AI vision,
- overseer actions,
- automatic docking,
- calibration,
- all direct action endpoints.

Operator preview and low-level safety vision may remain available during STOP, but must be explicitly separated from AI vision.

---

## 6. Root-cause the one-process full-suite hang

Per-file green results are not enough. Fix the lifecycle leak or deadlock so one canonical invocation exits cleanly.

### Diagnose deliberately

Use diagnostics such as:

```powershell
$env:PYTHONASYNCIODEBUG = "1"
python -X faulthandler -m pytest -vv -s --tb=short -p no:recording
```

Also use test-order bisection and session-finish diagnostics that dump:

- live non-daemon threads,
- pending asyncio tasks,
- event loops,
- child processes,
- open sidecar pipes/readers,
- active TestClient/application lifespan instances.

The timeout plugin may be used to capture a stack during diagnosis, but a timeout is not an acceptance fix.

### Known cleanup candidates

Inspect and correct at least:

- `SpeechService._schedule_clear()`: track created tasks, cancel and await them during shutdown/test teardown,
- server/application lifespan: close background AgentBrain tasks and shared clients,
- TestClient fixtures: use context managers and explicit close,
- `RtmNode.stop()`: terminate, wait/kill if needed, close pipes, fail pending commands, and join its manager/reader threads,
- sidecar child-process test readers: use a genuinely bounded reader thread/queue or selectors instead of a blocking `readline()` behind a decorative deadline,
- module-level globals reused across tests,
- any event loop created by imports or retained between tests.

### Acceptance for suite health

The canonical command must exit zero three consecutive times in fresh processes on Windows:

```powershell
1..3 | ForEach-Object {
    python -X faulthandler -m pytest -q -p no:recording
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

Requirements:

- no split-by-file substitution,
- no pending-task warnings,
- no socketpair/asyncio shutdown hang,
- no orphan Node or Python child process,
- no per-test timeout firing,
- exact counts and exit codes recorded in `agent_results.md`.

---

## 7. Add adversarial integration tests

Unit tests for the isolated arbiter are necessary but not sufficient. Add deterministic tests involving the endpoint, link/RtmNode, and real child-process sidecar with injected delays/barriers.

Required cases:

1. Two overlapping STOPs where the older send finishes first.
2. RESET attempted while the initial zero send is blocked.
3. Two simultaneous RESET requests.
4. STOP after the sidecar reset response but before process CAS commit.
5. Failed process CAS followed by acknowledged sidecar relatch.
6. Motion admitted before STOP and dispatched afterward.
7. Stale queued dock after STOP.
8. Stale queued dock-release/release/resume-control after STOP.
9. Stale queued speed/mode/avoidance/laser command after STOP.
10. Stale `set_control` queued behind connect.
11. STOP during connect/reconciliation.
12. Response from an old sidecar instance.
13. Lower epoch or generation rejected.
14. Reused RESET token rejected.
15. Raw hard-forbidden IDs remain forbidden despite environment configuration.
16. Voice STOP and barge-in STOP invoke one link E-STOP each.
17. STOP cancels blocked reasoning and prevents post-STOP tools/history/speech.
18. UI/capability heartbeat recovers STOP state after a dropped event.

The child-process test harness must have a real bounded timeout and reliable teardown on Windows.

---

## 8. Rewrite the hardware evidence harness before any use

The harness must never infer precise evidence from a generic `ok` field.

Required evidence remains separate:

- `queued_to_sidecar`,
- `sdk_send_succeeded`,
- `robot_effect_observed`.

Missing fields remain null/false with an explicit reason. Never auto-set physical effect evidence.

Additional requirements:

- reject dirty trees for acceptance,
- record exact commit SHA,
- record HTTP status and body,
- record process and sidecar state before/after,
- record dispatch and completion timestamps,
- abort on failed STOP or failed RESET,
- never produce PASS under `--auto`,
- verify stale-effect rejection and no post-STOP visual motion window,
- do not resume unless reconciliation is proven,
- add scripted harness unit tests.

Do not run the rewritten harness against the robot under this directive.

---

## 9. UI correctness after backend contracts stabilize

- Capability heartbeat must repair the local STOP display from authoritative `master_inhibited` state.
- STOP UI must distinguish:
  - locally inhibited,
  - transport stop dispatched,
  - degraded transport failure.
- RESUME must inspect HTTP status and reconciliation body.
- Failed RESUME keeps the UI stopped and displays the exact error.
- Generic request helpers must reject non-2xx responses instead of treating JSON as success.
- Requested ability state must never be shown as effective before authoritative capability data arrives.
- Build and verify the served bundle against the exact tested commit.

Required frontend commands:

```powershell
Set-Location webui
npm ci
npm run build
Set-Location ..
```

Record the served asset name, SHA-256, source commit, and stale flag in `agent_results.md`.

---

## 10. Documentation comes last

After implementation and tests are green:

- Correct `docs/CURRENT_STATE.md` to the actual committed SHA.
- Remove references to an old base plus an uncommitted tree.
- Do not claim every action passes SafetyKernel until the route audit proves it.
- Do not claim the test gate is complete until the single full-suite invocation exits cleanly three times.
- Keep Air 2 cloud/internet requirements explicit.
- Keep Max-class support marked unverified unless physically tested.
- Keep Phase 0 status FAIL until physical acceptance.
- Update `docs/PHASE0_ACCEPTANCE.md` with reproducible commands and actual counts, not stale fixed numbers.

---

## Required test sequence before reporting completion

At minimum, run and record:

```powershell
python -m pytest -q tests/test_control_arbiter.py tests/test_rtm_node.py tests/test_sidecar_protocol.py tests/test_estop_endpoint.py -p no:recording
python -m pytest -q tests/test_action_executor.py tests/test_safety.py tests/test_speech.py tests/test_bargein.py -p no:recording
python -m pytest -q -p no:recording
```

Then run the full suite three consecutive times as specified above.

Also run:

```powershell
git status --porcelain
git rev-parse HEAD
node --version
python --version
```

The final tested tree must be clean except for the intended final update to `agent_results.md`; commit that report after tests so its recorded tested SHA distinguishes the code-under-test SHA from the report commit SHA.

---

## `agent_results.md` required format

Use these exact top-level sections:

```markdown
# Agent Results

## Tested code commit SHA
## Report commit SHA
## Phase 0 verdict
## Hardware eligibility
## Summary
## Critical safety changes
## Files changed
## Exact test commands and exit codes
## Full-suite hang diagnosis and fix
## Sidecar integration evidence
## Reason-cancellation evidence
## Motion-ticket integration evidence
## Frontend build and served-bundle evidence
## Harness status
## Known limitations
## Working-tree status
```

Rules:

- `Phase 0 verdict` must remain FAIL unless supervised physical acceptance has separately passed.
- `Hardware eligibility` must remain NO until every software gate in this file is satisfied and reviewed.
- Include exact outputs or concise unambiguous excerpts, not “all tests look good.”
- Explicitly state skipped tests and why.
- Explicitly state whether Node child-process tests actually ran or were skipped.
- Explicitly state whether the canonical full-suite command exited normally three consecutive times.
- Do not erase known limitations to make the report prettier.

## Stop point

After all software work above is committed, the full suite exits cleanly three consecutive times, the frontend build is tied to the tested SHA, and `agent_results.md` is complete:

1. Do not run the robot.
2. Do not begin Phase 1.
3. Stop work and leave the complete report in `agent_results.md` for review.
