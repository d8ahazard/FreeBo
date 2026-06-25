# Agent Results

> WORK IN PROGRESS — `agent_next_5.md` ("Run the Supervised R4.0 Gate, Then Begin Phase 2 Benchmarking").
> Communicate only here. Phase 0 software stays ACCEPTED/FROZEN. **Hardware is NOT run by the agent**: the
> supervised R4.0 gate (§4) requires a physically-present human operator + an interactive arming ceremony, which
> the directive forbids auto-arming/inferring. The agent delivers the §1 preflight corrections + the honest §3
> R4.0 runner + tests, commits a clean preflight SHA, and STOPS at the physical boundary for the operator.

## Directive
`agent_next_5.md`: fix the §1 production-path preflight defects, build an honest supervised R4.0 runner (§3),
then (operator) run the R4.0 smoke gate (§4); conditionally begin a Phase 2 model benchmark (§5) ONLY if R4.0
passes. Hardware execution and Phase 2 are gated on a physically-present operator and a hardware PASS.

## Baseline SHA
- agent_next_4 tested code SHA `e7763058d6b7db23c8c2dc3032e7dbf44d623aa0`; final report
  `eaef25d5ed89ebb0bf421bd246a7ef9956096e97`.
- Canonical suite at baseline: 243 passed, 2 hardware-gated skips, exit 0. Real Node child-process tests ran.
  Software-only R4.0 rehearsal 12/12. Hardware was not run.
- Environment: win32 (Win 10.0.26200), Python 3.10.11, Node v22.16.0.

## Review disposition
Phase 1 is accepted; the §1 findings are real preflight corrections (no phase downgrade). Status of each is
tracked in **Preflight defects corrected** below.

## Preflight defects corrected
- **1.1 Manual motion drops the ticket id** — `/api/control` (manual + overseer) and the overseer calibration
  probe admitted a ticket but called `LINK.drive()/move()` with only epoch+generation, so the Air 2 native link
  (which mandates `ticket_id`) fails closed on the real robot. (pending)
- **1.2 Descending cursor pagination wrong** — `seq > cursor` was applied regardless of order; descending pages
  repeat newer rows. Fix to order-aware semantics + validate the cursor's session/sequence domain. (pending)
- **1.3 Recovery may restore the oldest tail** — streaming each file from its start + a global budget can restore
  a large active file's OLDEST rows and starve rotated history. Fix to bounded newest-tail recovery in true
  chronological order. (pending)
- **1.4 Writer shutdown can strand the writer** — a full queue makes `put_nowait(None)` fail so no sentinel
  reaches the writer; the join times out and the file can close while the daemon writer is alive. Fix so the
  writer always observes closure, drains to the deadline, and the file is never closed while it can still write.
  (pending)
- **1.5 Loopback trust unsafe behind a reverse proxy** — trusting `request.client.host==127.0.0.1` exposes
  observability through a local proxy. Fix: require the owner token whenever the configured bind is non-loopback,
  regardless of peer address; fail closed; never trust `X-Forwarded-For`/`Forwarded`. (pending)
- **1.6 Harness not yet an honest R4.0 gate runner** — addressed by the §3 rewrite. (pending)
- **1.7 Stale software counts in acceptance docs** — update `docs/PHASE0_ACCEPTANCE.md` to the current immutable
  evidence (243), preserving the old snapshot as history. (pending)

## Preflight tested code SHA
(pending)

## Preflight exact tests and exit codes
(pending)

## R4.0 arming and operator checklist
(pending — built by the agent; EXECUTED by the operator)

## R4.0 physical evidence SHA
NOT RUN by the agent. Requires a physically-present operator (§4).

## R4.0 trial results
NOT RUN.

## R4.0 acceptance report
NOT RUN.

## R4.0 verdict
**NOT RUN — pending supervised operator execution.** The directive forbids the agent from arming/inferring
presence; `--auto` is diagnostics-only and can never pass.

## Phase status
- Phase 0 software gate: ACCEPTED, FROZEN
- Phase 0 physical gate: PENDING (R4.0 not yet run)
- Phase 1 observability: COMPLETE FOR R4.0
- Phase 2 cognition/model benchmarking: BLOCKED (conditional on an R4.0 PASS, which requires the operator)
- Phase 3 personality: BLOCKED

## Conditional Phase 2 status
NOT ENTERED. §5 begins only after an R4.0 PASS in this wave; R4.0 was not run (no operator present).

## Benchmark architecture
(not entered)

## Benchmark candidates actually tested
(not entered)

## Benchmark results
(not entered)

## Recommended model stack
(not entered)

## Proposed configuration changes
(none applied; none proposed — Phase 2 not entered)

## Exact final tests and exit codes
(pending)

## Frontend build evidence
(pending)

## Machine-readable evidence paths
(pending)

## Known limitations
(pending)

## Working-tree status
In progress.
