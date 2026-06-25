# Agent Results

> Report for `agent_next_2.md` ("Close the End-to-End Safety Authority"). Communicate only here. Every claim is
> tied to a tested code SHA + exact command + exit code + platform/versions. WORK IN PROGRESS — this wave is
> being implemented in order; sections below fill in as commits land.

## Directive
`agent_next_2.md` — close the cross-process safety-authority gaps left by the prior wave: one strict control
protocol, prepared two-phase RESET, a synchronized RtmNode state machine, removal of every unticketed
robot-effect path, priority-first exactly-once STOP, complete reasoning/faculty cancellation, coherent dark/wake
lifecycle, a stronger (un-run) hardware harness, forced adversarial race tests, a static authority audit,
UI/API contract tests, and an honest report. No hardware. Phase 0 stays FAIL.

## Baseline commit
- Previous directive commit: `0fd1263091676cf910ab3e2a9f36632f2235fdd2`
- Previous tested-code commit (prior wave): `383bd8674eeda209f8e77627e0688f349246b4db`
- Previous report commit: `af9fedacaaad5422563a46dd910a0fa54ae7afb9`
- This wave starts from: `4c1a0cdbf02efdd1ea7bda27bd5e2ac1b274d927` ("Add Phase 0 authority closure directive").
- Environment: Node v22.16.0, Python 3.10.11, win32.

## Tested code commit SHA
(Filled at the final-verification step with the exact SHA the canonical suite was run against.)

## Report parent SHA
(The parent of this report's commit — i.e. the last code commit — recorded at finalization. A report cannot
embed its own commit SHA.)

## Phase 0 verdict
**FAIL.** Software work does not authorize hardware; supervised physical acceptance is separate and not run.

## Hardware eligibility
**NO.** No live robot / R4.0 / R4.10 / movement / hardware command is run under this directive.

## Review corrections
The prior wave's report (`af9feda`) over-claimed. Corrected scope BEFORE implementing this wave:
1. `set_control` is NOT correlated end-to-end — startup and `RtmNode.set_control()` still use fire-and-forget
   `_send()` and accept stdin-write success. (Fixed in §3.)
2. Non-drive effects do NOT consistently carry/enforce tickets; several still call `_send()` directly
   (`dock/avoid/laser/release/resume/dock_release/move_mode/move_speed`, overseer, callers). (Fixed in §4.)
3. Sidecar/process identity, epoch, and generation are OPTIONAL on important commands (accepted when absent),
   not mandatory. (Fixed in §1/§4.)
4. RESET is a single-phase remote unlatch (`estop_reset`) followed by a process CAS — an unsafe split-state
   interval where the sidecar is unlatched before the process validates. (Replaced by two-phase in §2.)
5. The full-cortex cancellation test (`test_stop_while_provider_blocked_yields_cancelled_no_drive`) is SKIPPED,
   and the single `_reason_task` pointer can cancel a waiter rather than the active provider-blocked cycle.
   (Fixed in §6.)
6. The hardware harness does not consume nested `transport_result` E-STOP evidence and lacks the full
   acceptance-threshold matrix. (Fixed in §8.)
7. `docs/PHASE0_ACCEPTANCE.md` and the report can disagree on the canonical test count; this wave records the
   ACTUAL count from the exact final SHA and keeps one canonical tested-SHA record (no memorized number).

Accomplishments from the prior wave are retained (see "Accepted from the prior wave" in the directive):
process-side `ControlArbiter` tokenized STOPs, epoch+generation advance per STOP, exclusive single-use process
RESET admission, `ActionExecutor` motion-ticket admit+revalidate, sidecar instance id, clean test-process
teardown, E-STOP API local-vs-transport distinction, UI not treating every response as success, harness
refusing to invent `robot_effect_observed`.

## Implemented transitions
§1 (commit `dd2d6d2`): one strict control protocol. Closed `EFFECT_*` class set (motion/dock/release/resume/
move_mode/move_speed/avoid/laser/eyes/speech/calibration); `EffectTicket{epoch,generation,effect_class,
ticket_id}` (MotionTicket = the motion alias). `ControlArbiter.admit_effect(class)` issues a unique non-zero
ticket id snapshotting the current transition, gated on not-inhibited/latched/STOP-in-flight; `validate_ticket`
requires exact epoch+gen + permitted. `SafetyFloor.admit_effect(class, source, settings)` is the single policy
authority (speech additionally gated by talk toggle + quiet). Evidence:
`pytest tests/test_control_arbiter.py` → 11 passed (incl. effect tickets classed/unique/invalidated-by-STOP).
Command-result semantics (protocol_valid / queued_to_sidecar / control_state_applied / sdk_send_* split) are
enforced at the sidecar + RtmNode boundary (§2/§3).

## Two-phase RESET evidence
§2 (commit `7c86e93`): single-phase remote unlatch REPLACED by a prepared two-phase release (no split-state
interval). `ResetToken` reserves a strictly-newer (release_epoch, release_generation); `arbiter.finalize_reset`
atomically installs it only if no STOP raced (`begin_reset` reserves, releases nothing). Sidecar `prepare_reset`
(validate identity+exact state+release-newer+control-ready; store one record + nonce; STAY latched; no SDK send)
+ `commit_reset` (re-validate same prepared reset, no STOP after prepare, atomically install + clear latch +
consume nonce). `set_control` can only ASSERT/preserve a latch, never clear it. `RtmNode.reset_reconcile`
(prepare→commit, fail-closed). `/api/resume`: sync-preflight + admission + link reconcile + finalize; a
post-commit STOP race stays inhibited and re-latches (degraded-critical). Sidecar STOP invalidates a prepared
reset; parent-pipe end / SIGTERM fail-safe latches + invalidates + zero-sends before exit. Evidence (forced
ordering, not sleeps): `pytest tests/test_control_arbiter.py tests/test_rtm_node.py tests/test_sidecar_protocol.py
tests/test_adversarial_integration.py tests/test_safety.py` → 57 passed; `tests/test_estop_endpoint.py` → 3
passed. Sidecar barrier cases proven: prepare-cannot-match-newer-STOP, STOP-after-prepare-invalidates-commit,
reused-nonce-rejected, set_control-cannot-unlatch, parent-death→new-instance-starts-latched.

## Remaining sections (in progress)
Landed + tested: §1 (`dd2d6d2`), §2 (`7c86e93`), §3 safe subset (`e4127a4`). NOT yet complete: §4 effect-ticket
enforcement on ALL routes + static audit, §5 priority-first STOP, §6 reasoning/faculty cancellation, §7 dark/wake
lifecycle, §8 harness acceptance calcs, §9 the remaining forced-race tests + FAKE-seam controls, §10 authority
audit, §11 UI/API contract, §12 evidence gate (3x clean), §13 final report. This report stays WORK IN PROGRESS
and Phase 0 stays FAIL until they land + the canonical suite runs 3x clean. Full-suite gate not yet re-run at
this SHA.

## Priority STOP evidence
(Pending — §5.)

## Effect-ticket audit
(Pending — §4 + §10 static audit.)

## Reason/faculty cancellation evidence
(Pending — §6.)

## Sidecar lifecycle evidence
§3 (commit `e4127a4`): RtmNode synchronized state machine (safe subset). Dedicated `_state_lock` (RLock) guards
protocol/identity/reset state (command_result adoption + `control_state()` snapshot). Correct startup ordering:
`_serve` reads + validates `ready` and binds that exact sidecar instance BEFORE connect/reconcile. Strict
pending-command correlation: a result with the right id but wrong kind can never satisfy a waiter or mutate
observed state. `synchronized` requires the accepted process id to EQUAL ours (no None allowance). Deterministic
shutdown joins both the manager and the now-tracked stderr thread. Evidence: `pytest tests/test_rtm_node.py` →
13 passed (incl. wrong-kind correlation). DEVIATION (documented): the connect-time reconcile still uses
fire-and-forget `set_control`, which the sidecar now permits ONLY to assert/preserve a latch (never unlatch); a
blocking correlated `set_control` on the manager thread would deadlock its own reader. Full correlated-ack
startup (thread-split) is deferred. §7 dark/wake lifecycle still pending.

## Harness status
Rewritten in the prior wave but NOT yet consuming nested transport evidence / full acceptance matrix
(correction 6). To be strengthened in §8. Never run against the robot under this directive.

## Exact test commands and exit codes
(Pending — §12: the three focused groups + the canonical full suite ×3 at the final SHA.)

## Machine-readable evidence paths
(Pending — committed summaries + raw outputs under `data/test-evidence/software/<tested-sha>/`.)

## Frontend build evidence
(Pending — §11/§12: `npm ci && npm run build`, full SHA-256 + source SHA + stale flag.)

## Known limitations
- HARDWARE NOT RUN. Phase 0 = FAIL, hardware eligibility = NO.
- This section will list every skipped test and any race not forced by a barrier once the wave completes.

## Working-tree status
In progress. Target: clean except this report, committed last so the tested-code SHA is distinct from the
report SHA. `webui/dist/` stays gitignored; committed evidence lives under `data/test-evidence/software/`.
