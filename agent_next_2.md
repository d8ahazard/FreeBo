# Agent Next 2: Close the End-to-End Safety Authority

This is the authoritative continuation of `agent_next.md` after review of the 12-commit wave ending at
`af9fedacaaad5422563a46dd910a0fa54ae7afb9`.

The prior wave made real progress. The process-side arbiter is substantially better, the full test process now
exits cleanly, the UI reports failures more honestly, and the report correctly keeps Phase 0 at FAIL. However,
the implementation still has cross-process authority gaps and several claims in `agent_results.md` are broader
than the code actually supports. This wave closes those gaps. It is not a documentation cleanup exercise.

## Communication contract

1. Communicate only through the existing repository-root `agent_results.md`.
2. Do not answer in chat, ask for permission between sections, or stop after writing a plan.
3. Update `agent_results.md` immediately with this review's baseline and blockers before changing code.
4. Work in small, reviewable commits in the exact order below.
5. Tie every result to the exact tested code SHA, exact command, exit code, platform, and versions.
6. Do not run a live robot, R4.0, R4.10, movement calibration, or any hardware command under this directive.
7. Do not begin Phase 1, model work, navigation expansion, persona work, or performance tuning.
8. Phase 0 remains **FAIL** and hardware eligibility remains **NO** until this wave is reviewed and then
   supervised physical acceptance passes separately.
9. A test name or commit message is not evidence that the asserted race was actually forced. Use barriers,
   blocked sends, exact event ordering, and negative assertions.
10. Do not weaken a safety invariant to preserve compatibility. Remove or migrate the bypass.

## Reviewed baseline

- Previous directive commit: `0fd1263091676cf910ab3e2a9f36632f2235fdd2`
- Previous tested-code commit: `383bd8674eeda209f8e77627e0688f349246b4db`
- Previous report commit: `af9fedacaaad5422563a46dd910a0fa54ae7afb9`
- Reported suite evidence: 178 passed, 3 skipped, three consecutive process-clean runs on Windows.
- Hardware status: not run.

## Accepted from the prior wave

Keep these designs unless a correction below explicitly supersedes part of them:

- `ControlArbiter` tokenizes overlapping process-side STOPs.
- Every process-side STOP advances epoch and generation.
- RESET admission is exclusive and single-use in the process arbiter.
- `ActionExecutor` obtains a motion ticket and revalidates before the link call.
- The sidecar announces a generated instance ID.
- The full Python test process now tears down cleanly.
- E-STOP API responses distinguish local inhibition from transport dispatch.
- The UI no longer treats every JSON response as success.
- The hardware harness refuses to invent `robot_effect_observed`.

## Corrections to the current report

Update `agent_results.md` before implementation. The current report must not continue to claim the following
as complete:

1. `set_control` is not yet correlated end-to-end. Startup and `RtmNode.set_control()` still use `_send()` and
   accept stdin-write success.
2. Non-drive effects do not consistently carry or enforce tickets. Several still call `_send()` directly.
3. Sidecar identity, process identity, epoch, and generation are optional on important commands.
4. The current RESET protocol is still a single-phase remote unlatch followed by a process CAS. It has an
   unsafe split-state interval.
5. The full-cortex cancellation test is skipped, and the current reason-task pointer can cancel a waiter instead
   of the active reason cycle.
6. The hardware harness does not yet consume nested E-STOP transport evidence or implement the full acceptance
   matrix.
7. `docs/PHASE0_ACCEPTANCE.md` and the final report disagree on the canonical test count. Use results, not a
   memorized number, and keep one canonical tested-SHA record.

Do not erase the accomplishments. Correct the scope of the claims.

---

# 1. Define one strict control protocol

Before patching individual branches, define the protocol as code-level data structures and invariants shared by
Python tests and sidecar tests.

## 1.1 Required transition identity

Every safety/control transition must carry all of:

- `process_instance_id`
- `sidecar_instance_id`
- `transition_epoch`
- `control_generation`
- `transition_id` or command-specific nonce

Missing identity is a protocol failure. Do not treat a missing field as compatibility mode.

Every physical effect command must also carry an admitted effect ticket with:

- epoch
- generation
- effect class
- admission ID or ticket ID

For motion, this may extend `MotionTicket`. For non-motion robot effects, add an `EffectTicket` or a common
`ControlTicket`. Do not pass loose optional integers through several layers and call that authority.

## 1.2 Monotonic transition rules

- A STOP always latches and sends zero motion, even if its transition identity is stale.
- A stale STOP must never lower the accepted epoch or generation.
- A newer STOP invalidates every prepared RESET and every older effect ticket.
- RESET/RESUME must create a new post-resume epoch and generation. Commands admitted before STOP or before the
  completed RESUME must remain stale forever.
- `set_control` may assert or preserve a latch. It must never clear a latch.
- Only the explicit RESET state machine may clear a latch.
- A fresh sidecar starts latched and control-not-ready.
- Parent-process loss, stdin close, protocol corruption, or instance replacement immediately latches, clears
  active effects, and sends a zero frame when transport is available.

## 1.3 Command result semantics

Do not overload `ok` or `sent_to_agora`.

Every correlated result must distinguish:

- `protocol_valid`
- `queued_to_sidecar`
- `control_state_applied` for local sidecar state mutations
- `sdk_send_attempted`
- `sdk_send_succeeded` for actual Agora sends
- `robot_effect_observed` only in supervised hardware evidence
- `error`
- sidecar dispatch and completion timestamps
- exact command ID and command kind
- process/sidecar instance IDs
- epoch and generation before/after when state changes

A local RESET mutation is not an Agora send. Remove `sent_to_agora=true` from RESET and reconciliation results.

---

# 2. Replace single-phase RESUME with a prepared two-phase release

The current flow lets the sidecar set `latched=false`, then asks the process CAS whether that was still valid.
Reasserting STOP afterward narrows the damage but does not make the release atomic. Replace it.

## 2.1 Process reset token

Extend `ResetToken` to reserve a unique post-resume state:

- reset attempt ID
- expected current epoch/generation
- release epoch = a new monotonic epoch
- release generation = a new monotonic generation
- expected process instance
- expected sidecar instance
- prepare nonce once returned by the sidecar

`begin_reset()` still requires:

- master inhibited
- process motion latched
- no STOP dispatch in flight
- no other reset active
- synchronized current sidecar identity/state

It must not release any process faculty.

## 2.2 `prepare_reset`

Add a correlated sidecar `prepare_reset` command.

The sidecar validates all fields as mandatory:

- current process instance
- current sidecar instance
- exact current epoch/generation
- requested new release epoch/generation are both strictly newer
- no active STOP dispatch
- RTM/control session ready
- no prepared reset already active

On success it stores one prepared reset record and returns a fresh nonce. It remains latched. It does not claim an
SDK send.

## 2.3 `commit_reset`

Add a correlated sidecar `commit_reset` command carrying the prepare nonce and exact old/new identities.

The sidecar commits only when:

- the same prepared reset is still active
- no STOP arrived after prepare
- accepted current state is still the expected old state
- process and sidecar identities still match
- RTM/control session is still ready

It then atomically:

1. installs the new release epoch/generation,
2. clears the sidecar latch,
3. consumes the nonce,
4. returns `control_state_applied=true`, `reconciled=true`, and the exact new state.

Any rejection leaves it latched and consumes or invalidates the stale attempt as appropriate.

## 2.4 Process finalization

After the sidecar commit response, the process finalizes release under its arbiter lock only when:

- the reset attempt is still active,
- no newer STOP occurred,
- the response is protocol-valid,
- process and sidecar IDs are exact,
- response epoch/generation equal the reserved release state,
- sidecar reports unlatched and control-ready.

Only then clear master inhibit and the process latch.

If finalization fails after a sidecar commit, remain inhibited and synchronously issue the current priority
E-STOP. Wait for its correlated zero-send result before returning. Surface a critical degraded state when the
relatch cannot be proven.

## 2.5 Parent-death fail-safe

On sidecar stdin end, parent process loss, logout, or replacement:

- latch immediately,
- invalidate prepared RESET,
- clear drive/effect timers,
- dispatch zero motion when possible,
- stop accepting effects.

Add a test that commits a release and then closes the parent pipe. The sidecar must return to latched state and
must not accept a subsequent effect from a different process instance without a full new reconciliation.

---

# 3. Make `RtmNode` a synchronized protocol state machine

## 3.1 Dedicated state lock

Add one `threading.RLock` protecting all of:

- process authoritative epoch/generation/latch
- bound sidecar instance
- observed sidecar epoch/generation/latch
- accepted process identity
- control-ready state
- prepared reset state
- reconnect/reconcile state

Do not read or write those fields outside the lock or an immutable snapshot.

## 3.2 Correct startup ordering

Current startup sends `connect` and fire-and-forget `set_control` before the reader has bound the `ready`
instance. Replace that ordering:

1. spawn child,
2. read and validate `ready` with mandatory sidecar ID,
3. bind that exact sidecar instance,
4. send `connect`,
5. await correlated connected/control-session readiness,
6. send correlated latch-preserving reconciliation,
7. if process policy wants an initially unlatched system, use the same prepared two-phase release protocol,
8. mark synchronized only after exact acknowledgment.

No `_send(set_control)` remains.

## 3.3 Strict pending-command correlation

Each pending command slot must record expected:

- command ID and command kind
- process instance
- sidecar instance
- epoch/generation or transition token
- expected result class: transport send vs local state mutation
- command-specific validator

A response must not satisfy a waiter when any expected field is missing or different.

Reject and log:

- missing sidecar ID
- missing or wrong command kind
- wrong process ID
- impossible epoch/generation
- duplicate completion
- response from an old sidecar
- unsolicited state mutation

Do not adopt sidecar state from a `command_result` before that result passes protocol and command-specific
validation. Unsolicited telemetry/state events must have their own narrow validators.

## 3.4 Exact synchronization

`control_state().synchronized` requires all of:

- a bound current sidecar ID
- exact accepted process ID, never `None`
- sidecar process ready
- sidecar control ready
- exact epoch
- exact generation
- exact latch
- no pending prepare/commit mismatch
- no STOP dispatch in flight

Unknown is not synchronized.

## 3.5 Honest result normalization

Replace the assumption `ok == sent_to_agora` with command-specific result normalization.

- RTM effects succeed when their SDK send succeeded.
- local reconciliation succeeds when its control state was applied and validated.
- E-STOP reports local latch and SDK zero-send separately.
- RESET reports reconciliation without inventing an Agora send.

Preserve all sidecar evidence in the returned result, including:

- `dispatch_ts`
- `completion_ts`
- `rtm_id`
- process/sidecar IDs
- before/after epoch/generation/latch
- retry evidence

## 3.6 Deterministic shutdown

`RtmNode.stop()` must:

- mark closing under lock,
- fail pending commands,
- close stdin,
- terminate child,
- wait with a bound,
- kill and wait if needed,
- close stdout/stderr,
- join manager and stderr-reader threads,
- clear instance/control-ready state.

Track the stderr thread instead of leaving it as an anonymous daemon.

---

# 4. Remove all unticketed robot-effect paths

The report admits this incompleteness, and the code contains more bypasses than the report lists.

## 4.1 No optional motion ticket

For a physical link, `move()` and `drive()` must reject missing epoch/generation/ticket. Remove the
`Air2NativeLink._ticketed()` fallback to current RtmNode state.

Tests and non-physical mock links may construct explicit test tickets. Production code gets no legacy bypass.

## 4.2 Central effect admission

Create one authority method for non-zero robot effects, such as:

```python
admit_effect(effect_class, source, settings) -> EffectTicket | denial
```

At minimum classify and gate:

- motion
- docking/go-home
- release/resume controller ownership
- movement mode/speed
- avoidance changes
- laser/actuator
- expressive eyes
- outbound speech/playback
- calibration movement

The ticket carries the current epoch/generation and is checked again immediately before writing to the sidecar.
The sidecar checks it a third time.

Define explicit exceptions:

- E-STOP and zero/deadman motion are always permitted.
- read-only telemetry/operator video are not effects.
- narrowly defined transport housekeeping may continue only when it cannot create movement or weaken safety.

Do not classify controller ownership, avoidance-off, docking, or actuator changes as harmless housekeeping.

## 4.3 Migrate `Air2NativeLink`

Remove direct production calls to `rtm._send()` and unticketed wrappers.

Specifically migrate:

- `dock`
- `avoid`
- `laser`
- `release`
- `resume`
- `dock_release` / `go_home`
- `move_mode`
- `move_speed`

Every one must be correlated, ticketed, and rejected during master STOP, stale identity, or unsynchronized state.

`action()` must not be a safety bypass. Make typed methods or a typed effect dispatcher that requires a ticket.

## 4.4 Fix all callers

Audit and migrate:

- `_apply_command("HOME")`
- overseer `action`, `eyes`, `connection`, `move_mode`, `move_speed`, and `say`
- calibration and probes
- automatic docking and low-battery return logic
- place navigation
- sleep/dark/wake
- any skill calling `link.action()` directly
- any use of `RtmNode._send`, `raw`, `dock`, `avoid`, `drive`, or `set_control`

Use a repository test that fails when production files outside the RtmNode implementation call private `_send()`
or raw physical effect IDs.

## 4.5 Sidecar enforcement

All effect commands require exact mandatory:

- process ID
- sidecar ID
- epoch
- generation
- effect ticket/admission ID

Reject missing fields, not merely stale fields.

The serialized queue must invalidate every stale effect, not just `drive`.

The active drive repeat captures and rechecks:

- generation
- epoch
- process ID
- sidecar ID
- ticket ID
- latch/STOP state

A same-generation but newer-epoch transition must kill the repeat.

---

# 5. Make STOP physically priority-first and exactly once

## 5.1 Do not delay true E-STOP behind regular preemption

The current master STOP path asserts the local gate, then awaits `executor.preempt()`, which issues a bounded
ordinary stop, and only afterward calls the link-level E-STOP. A slow ordinary stop can delay the real E-STOP.

Refactor the sequence:

1. synchronously assert process inhibit/latch and capture the exact `StopToken`,
2. invalidate reasoning and faculty work,
3. cancel TTS immediately,
4. begin the true priority `link.estop(token)` immediately,
5. cancel the executor action without waiting on another transport stop,
6. await/report the priority E-STOP result,
7. use ordinary deadman stop only as a supplemental action after priority E-STOP has started.

Add `ActionExecutor.cancel_active(reason, dispatch_stop=False)` or equivalent so cancellation does not insert a
regular stop ahead of the hard stop.

Measure from API/keyword acceptance to:

- sidecar queue receipt
- sidecar local latch
- initial SDK zero-send start
- initial SDK zero-send completion

## 5.2 STOP must never regress state

The sidecar currently assigns the incoming STOP epoch/generation directly. Change it so every STOP:

- latches and zeros regardless of staleness,
- never lowers accepted epoch/generation,
- reports whether its transition token was current, newer, or stale,
- keeps each active dispatch under its own dispatch ID.

An older STOP completing after a newer STOP must not report or install the newer STOP's evidence as its own, and
must not clear the newer active dispatch.

Track retry timers and cancel them during teardown. Keep retry results separate from the initial send.

## 5.3 Eliminate duplicate voice STOP

`feed_speech()` currently launches `emergency_stop()` and also posts a STOP command that later calls
`emergency_stop()` again.

Choose one fast path. The preferred behavior:

- recognized authorized STOP directly invokes the unified master STOP once,
- do not enqueue a second STOP command,
- emit acknowledgment/status from that same result.

Add exact call-count tests for:

- voice STOP
- barge-in STOP
- red-button/API STOP
- STOP tool call

Each source produces one process transition and one link E-STOP.

## 5.4 Do not self-cancel the STOP caller

When STOP is invoked from the current reason/tool task, do not cancel the task before it dispatches the hard
stop. Invalidate the reason token, cancel other reason tasks/waiters, execute STOP to completion, then terminate
the stale reason cycle without further side effects.

Add a test where the provider returns a STOP tool call. It must dispatch one E-STOP and must not be interrupted by
its own task cancellation before the zero send.

---

# 6. Complete reasoning and faculty cancellation

## 6.1 Track all reason invocations correctly

A single `_reason_task` pointer is unsafe because a second caller waiting on `_reason_lock` can overwrite the
actual running task.

Track:

- the actual lock-owning reason task,
- waiting reason tasks, or a set of all active reason invocations,
- the generation token for each.

Master STOP and Think-off cancel all stale reason invocations except the task currently executing the STOP path.
Do not allow a waiter to hide the provider-blocked owner.

## 6.2 Use live settings at every guard

`_reason_guard` must consult a fresh settings snapshot/capability generation. A cycle-start snapshot must not let
Think, See, Speak, Listen, or Move continue after its toggle changes.

Add a capability-policy revision counter if needed so an in-flight cycle can detect any relevant toggle change.

## 6.3 Guard every side-effect boundary

Check the live reason token and relevant faculty:

- after every await
- before and after perception hooks
- before mutating `last_observation`, behavior, curiosity, history, memory, tasks, or status
- before and after provider/VLM/omni calls
- before emitting thoughts/tool calls/results
- before and after each tool execution
- before speech
- before motion/effects
- before storing captions
- before scheduling or firing tasks

A guard before an await is not sufficient. STOP can land during the await.

Pass the token into VLM and omni paths or wrap every effect they can produce. Returning from those functions must
be followed by a guard before status/history/output mutation.

## 6.4 Separate AI vision intake from operator preview

During master STOP or See-off:

- operator video may remain available,
- low-level safety observation may remain if explicitly separated,
- AI perception buffer intake, AI captioning, scene curiosity, identity processing, and model vision stop.

The current perceiver still fills the AI buffer under STOP. Route AI intake through `check_see()` and maintain a
separate operator/safety frame path.

If a caption/VLM request is in flight when See is disabled or STOP lands, cancel it or discard its result after a
live guard. A stale caption must not be stored, emitted, or fed to curiosity.

## 6.5 Listen and scheduler behavior

Before `feed_speech()` mutates behavior, heard state, transcripts, or queues, require live Listen permission.
The only exception is the dedicated critical STOP/QUIET detector, which routes directly to its safety path.

While master-inhibited or Think-off:

- scheduler may retain due tasks,
- it must not emit autonomous task-fired effects, mutate memory as though executed, or enqueue reasoning.

On RESUME, process retained tasks according to an explicit policy without replay storms.

## 6.6 Speech and expressions

- Overseer speech must use `SpeechService` and `check_say`, not a direct publish path.
- Turning Speak off or STOP landing must cancel active render/publish/playback and prevent a stale render result
  from publishing afterward.
- Autonomous eye expressions are robot effects and require current authority. They must not leak from stale
  reasoning after STOP.
- Operator eye control, if retained during STOP, must be an explicit documented operator-only policy rather than
  an accidental `link.action()` bypass.

## 6.7 Remove the skipped cancellation test

Make `test_stop_while_provider_blocked_yields_cancelled_no_drive` deterministic and mandatory. Force the brain
mode and test fixtures so it always reaches the mocked provider. No conditional skip.

Add tests for:

- two concurrent reason callers, one running and one waiting, then STOP
- STOP tool call from the running reason task
- Think off during provider await
- See off during caption await
- STOP after tool returns but before tool-result/history mutation
- stale result after RESUME
- scheduler due item during STOP
- addressed `/api/chat` during STOP returns an explicit non-2xx inhibition result and mutates no transcript/history

---

# 7. Repair control cadence and dark/wake lifecycle

## 7.1 Sidecar cadence under latch

The sidecar cadence currently sends controller ownership and avoidance-off independently of the ticketed effect
path.

Define the policy explicitly:

- RTM connection and read-only telemetry may remain alive during STOP.
- Do not send avoidance-off, docking, speed/mode, actuator, or control-release/resume effects while latched unless
  a specific safety transition authorizes them.
- If ownership keepalive must remain to preserve emergency control, document and test it separately.
- Prefer safety-preserving avoidance while stopped.

## 7.2 Fix `connection("stop"/"start")`

The current Air 2 path stops `RtmNode`, then attempts to send `release`; wake then sends `resume` to a stopped
manager without restarting it.

Implement a coherent lifecycle:

- `pause_control`: ticketed stop/release in the correct order while RtmNode is alive, then pause media as needed.
- `resume_control`: start/reconnect RtmNode if stopped, bind a new sidecar instance, reconcile latched state, and
  use the explicit release protocol only when policy permits.
- Never return `ok=true` before the requested lifecycle state is correlated and observed.

Add dark→wake, wake failure, sidecar replacement during dark, and STOP-while-dark tests.

---

# 8. Strengthen the hardware evidence harness without running it

## 8.1 Consume nested evidence

`/api/estop` carries transport evidence inside `transport_result`. The harness currently classifies mostly
 top-level fields.

Normalize nested and top-level evidence without inference:

- command ID
- queued-to-sidecar
- local sidecar latch
- initial SDK zero-send attempted/succeeded
- retry attempts/results
- sidecar dispatch/completion timestamps
- API receipt/completion timestamps
- process/sidecar identity and epoch/generation

Do not substitute `transport_dispatch_succeeded` for the individual facts.

## 8.2 Strict HTTP handling

- A missing HTTP status is failure, never acceptable RESUME.
- Non-JSON responses preserve status and raw body.
- Require exact successful status and exact reconciliation fields.
- Close the HTTP client deterministically.
- Perform clean-tree and tested-commit preflight before any movement, not only when saving.

## 8.3 Implement actual acceptance calculations

The harness must calculate and fail on the defined gates:

- STOP p95 under 600 ms using the correct endpoint-to-initial-zero reference
- STOP/QUIET TTS cancellation under 300 ms
- command acknowledgment p95 under 1.2 s
- motion dispatch under 250 ms after authorization
- required trial counts
- every STOP physically observed to halt
- no post-STOP motion during a defined observation window
- stale effect rejection
- explicit reconciled RESUME between trials
- all five faculties inhibited while operator camera/telemetry remain available

Keep `robot_effect_observed` operator-entered. The harness may gather camera/telemetry evidence but must label it
separately from human observation.

## 8.4 Full scenario support

Add scripted setup/cleanup for, but do not execute:

- held manual drive
- active turn
- ActionExecutor move
- queued stale effects
- active TTS
- active STT
- active caption/VLM request
- active cortex provider request
- RTM reconnect
- sidecar replacement

The current prompt asking the operator to somehow create “several drives in flight” is not a deterministic
scenario.

Add pure and mocked-client tests for every abort rule and threshold calculation.

---

# 9. Add adversarial protocol tests that force the races

Extend the FAKE sidecar test seam with deterministic controls such as:

- block a specified RTM send until a release command
- delay a command by ID
- pause queue pumping after dequeue
- expose active STOP IDs and prepared RESET state in test-only diagnostics

Do not use sleeps as the primary race mechanism.

Required tests:

1. Older STOP blocks in SDK send, newer STOP begins and completes, older STOP completes last. State never regresses
   and each result retains its own token/evidence.
2. Lower epoch/generation STOP still latches+zeros but never lowers accepted state.
3. RESET prepare while STOP initial zero send is blocked is rejected.
4. STOP after RESET prepare invalidates the nonce.
5. STOP after sidecar RESET commit but before process finalization leaves process inhibited and proves relatch.
6. Two simultaneous RESET prepares: exactly one accepted.
7. Reused prepare nonce rejected.
8. Missing process ID, sidecar ID, epoch, generation, or ticket rejected for every effect class.
9. Same generation but stale epoch drive rejected and active repeat killed.
10. Stale queued dock, dock-release, release, resume, speed, mode, avoid, laser, and eyes rejected after STOP.
11. `set_control(latched=false)` rejected. Only RESET may unlatch.
12. Startup reconcile waits for `ready` and uses a correlated result.
13. Missing sidecar ID in a command result cannot satisfy a Python waiter.
14. Correct command ID but wrong command kind cannot satisfy a waiter.
15. Same sidecar result with wrong process ID cannot mutate observed state.
16. Sidecar stdin close while unlatched causes latch+zero.
17. Voice STOP, barge-in STOP, API STOP, and STOP tool each dispatch exactly once.
18. Priority E-STOP starts before a blocked ordinary executor stop completes.
19. Provider-blocked active reason plus waiting reason are both cancelled by STOP.
20. Dark→wake performs a real restart/reconcile and cannot move before it.

No required test may skip when Node is installed. Node absence may skip the child-process subset only, and the
final report must state it prominently.

---

# 10. Static authority audit

Add an automated test or audit script that scans production code and fails on unauthorized patterns, including:

- `rtm._send(` outside `rtm_node.py`
- physical-effect `raw(` calls
- direct `link.move/drive/action/dock` from unapproved modules
- `set_control` through fire-and-forget transport
- physical effect methods with optional tickets
- `sent_to_agora=true` on local-only state changes

Keep an explicit allowlist small and documented. This does not replace runtime tests; it prevents the bypasses
from quietly returning six commits later, as software traditions demand.

---

# 11. UI and API contract tests

After backend contracts stabilize:

- `/api/resume` must expose prepare, commit, finalization, and any relatch failure without claiming success.
- `/api/chat` while inhibited returns 409 or 423 and does not report `queued=true`.
- effect endpoints return non-2xx when denied.
- STOP response preserves full nested transport evidence.
- readiness distinguishes preparing-reset, committing-reset, synchronized-unlatched, and degraded-relatch.
- UI remains STOPPED through every failed or pending RESET state.
- UI must never enable physical controls from requested settings alone.

Add backend endpoint tests and frontend tests for all states. Build the exact tested frontend and record full
SHA-256 values, not a truncated digest.

---

# 12. Test and evidence gate

## 12.1 Required focused commands

Run at minimum:

```powershell
python -X faulthandler -m pytest -q -p no:recording `
  tests/test_control_arbiter.py `
  tests/test_rtm_node.py `
  tests/test_sidecar_protocol.py `
  tests/test_adversarial_integration.py `
  tests/test_estop_endpoint.py

python -X faulthandler -m pytest -q -p no:recording `
  tests/test_action_executor.py `
  tests/test_reason_cancellation.py `
  tests/test_safety.py `
  tests/test_speech.py `
  tests/test_bargein.py

python -X faulthandler -m pytest -q -p no:recording `
  tests/test_hardware_harness.py
```

Add new files as needed; do not hide them from the focused commands.

## 12.2 Canonical full-suite gate

On the final tested code SHA, run the entire suite in three fresh processes:

```powershell
1..3 | ForEach-Object {
    python -X faulthandler -m pytest -q -p no:recording
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

Requirements:

- zero failures
- zero unexpected skips
- the provider-blocked reason test runs and passes
- Node child-process tests run and pass
- no pending-task warnings
- no orphan Python/Node child processes
- no timeout plugin firing
- clean exit all three times

Do not hard-code an expected pass count. Record the actual count from the exact final SHA.

## 12.3 Evidence files

Do not hide all evidence behind `.gitignore`.

Commit compact machine-readable software-run summaries under:

`data/test-evidence/software/<tested-sha>/`

Each summary must include:

- tested code SHA
- dirty/clean status before run
- exact command
- start/end UTC timestamps
- environment versions
- exit code
- pass/fail/skip counts
- Node child-process executed/skipped status
- SHA-256 of the full raw output

The quiet full-suite output is small enough to commit. Either commit the three raw outputs too, or state where an
immutable retrievable artifact exists. A hash of a deleted local file is not independently useful evidence.

## 12.4 Frontend

Run:

```powershell
Set-Location webui
npm ci
npm run build
Set-Location ..
```

Record complete SHA-256 hashes and source SHA. Verify the server reports that exact source and `stale=false`.

---

# 13. Final `agent_results.md` requirements

Replace the old final report with a current report. Preserve a concise prior-wave summary but remove inaccurate
claims.

Use these top-level sections:

```markdown
# Agent Results

## Directive
## Baseline commit
## Tested code commit SHA
## Report parent SHA
## Phase 0 verdict
## Hardware eligibility
## Review corrections
## Implemented transitions
## Two-phase RESET evidence
## Priority STOP evidence
## Effect-ticket audit
## Reason/faculty cancellation evidence
## Sidecar lifecycle evidence
## Harness status
## Exact test commands and exit codes
## Machine-readable evidence paths
## Frontend build evidence
## Known limitations
## Working-tree status
```

Notes:

- A report cannot truthfully embed its own commit SHA without creating another commit and changing it again.
  Record the tested code SHA and the report's parent SHA; the containing commit is visible in Git history.
- Phase 0 remains FAIL.
- Hardware eligibility remains NO.
- Explicitly list every skipped test.
- Explicitly state whether each child-process race was forced with a barrier/blocker.
- Do not call an endpoint or helper “fully ticketed” while any physical route has optional or missing tickets.
- Do not call a local state mutation an SDK send.
- Do not claim this software wave authorizes hardware. Review does.

---

# Stop point

Stop only when:

1. all sections above are implemented in reviewable commits,
2. all required adversarial races are deterministically proven,
3. the provider-blocked cancellation test no longer skips,
4. all physical effects are centrally admitted and sidecar-enforced,
5. RESET is prepared/committed rather than remotely unlatched before process validation,
6. priority E-STOP is not delayed behind ordinary preemption,
7. the full suite exits cleanly three consecutive times,
8. `agent_results.md` is complete and honest.

Then:

- do not run hardware,
- do not begin Phase 1,
- do not declare Phase 0 PASS,
- leave the repository ready for review.
