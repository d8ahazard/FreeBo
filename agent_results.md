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
(Pending — §1.)

## Two-phase RESET evidence
(Pending — §2.)

## Priority STOP evidence
(Pending — §5.)

## Effect-ticket audit
(Pending — §4 + §10 static audit.)

## Reason/faculty cancellation evidence
(Pending — §6.)

## Sidecar lifecycle evidence
(Pending — §3 + §7.)

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
