# Agent Results

> Living report for the Phase 0 software-gate directive (`agent_next.md`). Updated after each meaningful
> commit; stale claims are replaced, not appended. Every claim is tied to a commit SHA + exact command +
> exit code. WORK IN PROGRESS.

## Tested code commit SHA
In progress. Baseline at start of this directive: `0fd1263091676cf910ab3e2a9f36632f2235fdd2` (clean tree).
Last prior implementation commit: `efda9b60facfcd8fcdfe43611de9b439d20675bd`.

## Report commit SHA
(To be filled when this report is committed last, after the code under test.)

## Phase 0 verdict
**FAIL.** Phase 0 cannot pass on software alone; it requires supervised physical acceptance which is NOT run
under this directive.

## Hardware eligibility
**NO.** Remains NO until every software gate in `agent_next.md` is satisfied and reviewed.

## Summary
Implementing the Phase 0 software gate in order: (1) evidence hygiene, (2) sidecar atomicity + instance
identity + correlated set_control + validate-before-unlatch + ticketed effect commands + hard-forbidden raw,
(3) MotionTicket wired to the real transport boundary on every physical route, (4) real reasoning
cancellation + faculty inhibition, (5) truthful readiness + single-authority routing, (6) root-cause the
one-process full-suite hang, (7) adversarial integration tests, (8) hardware-harness rewrite, (9) UI
correctness, (10) docs. No live robot movement, no R4.0/R4.10, no `--auto` acceptance.

## Critical safety changes
(Filled per section as landed. Prior baseline `efda9b6`: process-side ControlArbiter — tokenized overlapping
STOPs, monotonic epoch+generation, exclusive single-use RESET admission + CAS, stale motion-ticket detection,
honest E-STOP API status.)

## Files changed
(Filled from `git diff --stat` against the baseline when reporting.)

## Exact test commands and exit codes
(Filled with the required command sequence + the 3x full-suite run, each with its exit code.)

## Full-suite hang diagnosis and fix
KNOWN BLOCKER (open): the one-process full `pytest` invocation intermittently hangs on Windows at
`socket.socketpair()` (asyncio loop creation) when the heavy modules collide. Per-file runs pass. Root cause
+ fix pending (Section 6).

## Sidecar integration evidence
Pending (Section 2 + 7).

## Reason-cancellation evidence
Pending (Section 4).

## Motion-ticket integration evidence
Pending (Section 3).

## Frontend build and served-bundle evidence
Pending (Section 9).

## Harness status
Pending rewrite (Section 8). The existing `scripts/hardware_smoke.py` must not be used for acceptance until
rewritten; it will never be run against the robot under this directive.

## Known limitations
- Full-suite single invocation hang (Section 6) — open.
- MotionTicket not yet enforced at the transport boundary / sidecar (Sections 2, 3) — open.
- Reasoning cancellation is generation-bump only, not boundary-checked (Section 4) — open.
- Readiness still uses coarse signals in places (Section 5) — open.

## Working-tree status
In progress. Target: clean except the final intended update to this file.
