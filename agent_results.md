# Agent Results

> `agent_next_5.md`. The agent completed the §1 preflight corrections + the honest §3 supervised R4.0 runner +
> tests and ran the §2 preflight software gate. **Hardware was NOT run**: §4 (the supervised R4.0 smoke) requires
> a physically-present operator + an interactive arming ceremony, which the directive forbids the agent from
> auto-arming or inferring. The robot is untouched; the gate awaits the operator. §5 (Phase 2) stays BLOCKED.

## Directive
`agent_next_5.md`: fix the §1 production-path preflight defects, build an honest supervised R4.0 runner (§3), then
(operator) run the R4.0 smoke (§4); conditionally begin a Phase 2 model benchmark (§5) ONLY after an R4.0 PASS.

## Baseline SHA
- agent_next_4 tested code SHA `e7763058d6b7db23c8c2dc3032e7dbf44d623aa0`; report
  `eaef25d5ed89ebb0bf421bd246a7ef9956096e97`. Suite 243 passed / 2 hardware-skips / exit 0; rehearsal 12/12.
- Environment: win32 (Win 10.0.26200), Python 3.10.11, Node v22.16.0.

## Review disposition
Phase 1 accepted. All seven §1 findings were real and are corrected as preflight (no phase downgrade). The 1.1
static audit additionally uncovered FIVE more un-ticketed production motion call sites beyond the three named.

## Preflight defects corrected
- **1.1 Manual/auto motion dropped the ticket id — FIXED.** Every production `drive()/move()` now passes the full
  ticket (epoch+generation+ticket_id): `/api/control` (manual + overseer), the overseer calibration probe, and
  (caught by the new audit) `locomotion.turn/step/reverse`, `motion_profile` calibration, and `go_to_place`. The
  static authority audit now FAILS on any production motion call carrying epoch/generation without ticket_id. New
  API contract test proves manual Air 2 motion reaches the fake sidecar with the full ticket; a partial ticket
  fails closed before transport.
- **1.2 Descending cursor pagination — FIXED.** Order-aware (`asc: seq>cursor`, `desc: seq<cursor`); the opaque
  cursor preserves + reports its session; a malformed cursor raises → the API returns HTTP 400. Tests: asc/desc
  3-page walks with no dup/omission, malformed 400, foreign-session resume by durable seq.
- **1.3 Newest-tail recovery — FIXED.** Retained files processed in true chronological order; the newest
  `recover_max_events` recovered via bounded reverse-line reading (not the first records); persistent queries
  scan each file's newest tail so a large active file can't starve rotated history. Tests added.
- **1.4 Writer shutdown — FIXED.** The writer ALWAYS observes closure via `_closing` (a full queue can drop the
  sentinel); the file is closed by the writer itself (never under a live writer); exact undrained count;
  idempotent repeat close. Deterministic full-queue shutdown test (blocked writer is not force-closed mid-write).
- **1.5 Reverse-proxy access — FIXED.** The access decision keys on the CONFIGURED bind, not the peer: a loopback
  bind is allowed; a non-loopback bind requires `AUTOBOT_OWNER_TOKEN` via `X-Owner-Token` for EVERY request
  (fail-closed; `X-Forwarded-For`/`Forwarded` never trusted). A proxy-forwarded 127.0.0.1 peer cannot bypass.
- **1.6 R4.0 runner — DONE.** See §R4.0 arming and runner.
- **1.7 Stale acceptance docs — FIXED.** `docs/PHASE0_ACCEPTANCE.md` now references the current immutable evidence
  (243), with the old 162-pass snapshot kept only as history.

## Preflight tested code SHA
**`48396440a58bc22ad68e01b5427ad730e095a932`** (`4839644`) — the clean commit the focused groups, full suite,
rehearsal, and frontend build were all run against. The hardware run MUST reference this exact SHA. Evidence
commit `662ee53`; this report is committed last (distinct SHA).

Commit wave: `2b3a912` baseline · `ee76803` 1.1 · `3aca23d` 1.2–1.5 · `4839644` 1.6/§3 runner + 1.7 docs ·
`662ee53` preflight evidence.

## Preflight exact tests and exit codes
- Focused groups: `pytest -q -p no:recording test_authority_audit test_api_contract test_observability
  test_observability_api test_hardware_harness test_rtm_node test_sidecar_protocol` → **92 passed, exit 0**
  (real Node child-process tests ran).
- Rehearsal: `python scripts/phase1_rehearsal.py` → **12/12 PASS, exit 0** (mock link + real Node FAKE sidecar
  only; `ready_for_supervised_R4_0=true`).
- Canonical full suite: `python -X faulthandler -m pytest -q -p no:recording` → **260 passed, 2 skipped, exit 0**
  (~88s). Skipped (both intentional, `--hardware`-gated): `tests/test_hardware.py` [1] and [2].

## R4.0 arming and operator checklist
The runner (`scripts/hardware_smoke.py --mode r4_0 --armed`) refuses ALL motion until every arming condition
passes (§3.1): `--mode r4_0` + `--armed` + the typed presence phrase `I AM PHYSICALLY PRESENT` + a clean git tree
+ the exact tested preflight SHA (`--expect-sha 4839644`) + the running app reporting the SAME software SHA + a
live AIR2 link + synchronized process/sidecar control + a healthy journal writer + the operator confirming the
7-item physical safety checklist (flat floor, 2 m clear radius, hazards excluded, operator within reach, STOP
visible, intervention path, battery/telemetry/video live). `--auto` is diagnostics-only and can never arm or pass.
Caps: forward ≤0.20, turn ≤0.18, duration ≤0.60 s, normal stop after each ordinary motion, explicit reconciled
RESUME after each master STOP, freshness gate, never an unbounded held drive.

## R4.0 physical evidence SHA
**RUN once (operator-supervised), ABORTED.** Software SHA `ae6e71eb7478a62bfea6bc1cf039948dbd8c9d28` (clean tree,
app-reported SHA matched, AIR2 link, synchronized). Evidence:
`data/test-evidence/hardware/ae6e71eb7478a62bfea6bc1cf039948dbd8c9d28/r4_0/20260625-181247/` (manifest.json +
rows.jsonl, 26 rows). Arming: all conditions + the 7-item checklist confirmed (`armed_ok=true`). Pre-run control
reconcile (STOP→RESUME) succeeded and live-validated the E-STOP transport (initial-zero sent, ack 172 ms,
two-phase resume reconciled).

## R4.0 trial results
- 5 eyes, 5 forward (0.20/0.4 s), 5 turn (0.18/0.4 s), 10 normal stops: all dispatched; operator observed each
  motion **halt, with NO post-stop motion and NO unexpected motion**.
- 1st master-STOP trial (forward_pulse scenario): the STOP was correct at every layer (`local_inhibit=true`,
  `local_latch_set=true`, `initial_zero_sdk_send_succeeded=true`, retry_count=3, gen/epoch 2→3, sidecar adopted,
  re-`synchronized=true`), but the operator could not confirm a physical halt (`motion_started_observed=false`,
  `halt_observed=?`) → the harness ABORTED (unknown is not a pass) and issued a priority E-STOP. Trials 2–10 not
  run.

## R4.0 acceptance report
`pass=false`. `counts_ok=false` (aborted after 1/10 master-STOPs). STOP latency p95 281 ms (≤600 ✓), ack p95
766 ms (≤1200 ✓), **motion_dispatch p95 1031 ms (≤250 ✗)**, journal healthy throughout. No safety gate was
violated; the gate failed to COMPLETE, it did not record an unsafe event.

## R4.0 verdict
**ABORTED on the first master-STOP — "STOP halt not observed."** NO confirmed safety failure: the STOP path
asserted inhibit+latch and dispatched on every attempt, every observed motion halted, and no post-stop or
unexpected motion was reported. Root cause (see below). Robot left **latched + inhibited**. Per the directive,
the wave stops here; only the observed issue is to be fixed; **no broad refactor; Phase 2 NOT entered.**

### Root cause (from evidence, not inference)
1. **Cloud motion-dispatch latency ~0.5–1.0 s (p95 1031 ms vs 250 ms target).** Drive commands reach the robot
   over the Agora/RTM cloud path with high, variable lag.
2. **The 100 ms drive keepalive repeat** (sidecar `driveRepeat`, by design so the robot's deadman doesn't cut a
   pulse) bunches under that lag, so a short 0.4–0.6 s pulse presents as several discrete same-direction nudges —
   the operator's "repeating itself." It is latch-safe (each repeat checks `!latched`), so every pulse still
   halted.
3. **Harness staging flaw:** the master-STOP scenario fires a 0.6 s pulse then immediately the STOP; under ~1 s
   dispatch lag the pulse auto-zeroes (its `driveStopTimer`) BEFORE the STOP lands, so there is no active motion
   to halt → the operator cannot observe a STOP-arresting-motion → correct abort.

### Narrow fix candidates (not yet applied — operator to choose; no broad refactor)
- Harness: for STOP scenarios, keep the robot in motion until the STOP fires using back-to-back capped pulses
  (each still ≤0.20/≤0.18 mag, ≤0.6 s, no unbounded held drive) so a STOP-during-motion is observable.
- Investigate/measure the cloud motion-dispatch latency (the dominant real issue) before re-running R4.0.
- Neither touches the safety floor or the frozen Phase 0 invariants.

## Phase status
- Phase 0 software gate: ACCEPTED, FROZEN
- Phase 0 physical gate: **R4.0 ABORTED (no safety failure; staging/latency)** — re-run required
- Phase 1 observability: COMPLETE FOR R4.0
- Phase 2 cognition/model benchmarking: **BLOCKED** (requires an R4.0 PASS — not achieved)
- Phase 3 personality: BLOCKED

## Conditional Phase 2 status
NOT ENTERED. §5 begins only after an R4.0 PASS in this wave; R4.0 was not run (no physically-present operator).

## Benchmark architecture
Not entered (Phase 2 gated on R4.0 PASS).

## Benchmark candidates actually tested
None (Phase 2 not entered).

## Benchmark results
None (Phase 2 not entered).

## Recommended model stack
None (Phase 2 not entered).

## Proposed configuration changes
None proposed and none applied (Phase 2 not entered). No production model configuration was changed.

## Exact final tests and exit codes
As in "Preflight exact tests and exit codes" above (focused 92, full suite 260/2-skip, rehearsal 12/12; all exit
0). No physical trials were run.

## Frontend build evidence
`cd webui ; npm ci ; npm run build` → tsc clean, exit 0. Byte-identical to agent_next_4 (no webui changes this
wave): entry `assets/index-BR_lsTOm.js`
sha256 `FA856A1B887E068076E8ECF4A1D4FEE7671F18D164215D5759AA9497FADEE91B`; CSS `assets/index-BfsfA-T8.css`
sha256 `85C8A1DB3716BA29103596CF06CEE1ABBC9C350D3D1EF95DBBEF5EB50CE03AE7`; `index.html`
sha256 `BBF1B84DA0B6EEE1D3E52F79F477C6051697AD4AAD5590DCE95C992E3B5F65E4`.

## Machine-readable evidence paths
Under `data/test-evidence/software/48396440a58bc22ad68e01b5427ad730e095a932/`:
- `summary.json` — counts, exit codes, env, frontend hashes, defects corrected, hardware status.
- `fullsuite.txt` — raw full-suite output (260 passed / 2 skipped / exit 0).
- `rehearsal/rehearsal_report.json` + `rehearsal/scenario_*.json` — 12/12 redacted bundles.

## How the operator runs the supervised R4.0 gate (handoff)
1. Place the EBO Air 2 per the physical safety checklist; keep the UI STOP visible and stay within reach.
2. Check out the exact tested SHA and confirm a clean tree: `git checkout 4839644` (no local edits).
3. Start the app against the live robot (AIR2 link); confirm `/api/hardware_gate` reports `software_sha` =
   `48396440a58bc22ad68e01b5427ad730e095a932`, `journal_health.writer_alive=true`, and readiness
   `synchronized=true`.
4. Run: `python scripts/hardware_smoke.py --mode r4_0 --armed --expect-sha 48396440a58bc22ad68e01b5427ad730e095a932 --base http://127.0.0.1:8200`
   (add `--owner-token <token>` if the app binds non-loopback). Type the presence phrase + answer every physical
   observation honestly (`?` = unknown = not a pass).
5. On PASS, record: Phase 0 physical gate = R4.0 SMOKE PASSED; R4.10 PENDING; Phase 2 = AUTHORIZED. On FAIL/ABORT,
   the robot is left latched + inhibited; fix only the observed issue and stop. A PASS is NOT authorization for
   R4.10. Phase 2 only begins after a recorded R4.0 PASS.

## Known limitations
- The agent cannot perform §4 (physical R4.0) or §5 (Phase 2 benchmark) — both require a physically-present
  operator / an R4.0 PASS. Delivered: the preflight corrections + the runner + tests, ready to execute.
- Deterministic STOP scenarios are orchestrated through EXISTING APIs (capped move + connection bounce); no new
  motion-capable endpoint was added (the directive's optional gated endpoint was intentionally avoided to keep the
  motion attack surface minimal). The "executor_move" scenario is approximated by a longer capped move.
- The connect-time `set_control` remains assert-latch-only (documented; unchanged).

## Working-tree status
All code + evidence committed (tested preflight SHA `4839644`; evidence `662ee53`). This report is committed last
(distinct SHA). `webui/dist/` is a gitignored build artifact (hashes above). The robot was not contacted; no
hardware was run.
