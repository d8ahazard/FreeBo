# Agent Next 5: Run the Supervised R4.0 Gate, Then Begin Phase 2 Benchmarking

This directive moves FreeBo forward.

The `agent_next_4` wave is accepted as completing Phase 1 observability for the supervised R4.0 gate.

Accepted evidence:

- Tested code SHA: `e7763058d6b7db23c8c2dc3032e7dbf44d623aa0`
- Final report commit: `eaef25d5ed89ebb0bf421bd246a7ef9956096e97`
- Canonical suite: 243 passed, 2 intentionally hardware-gated skips, exit 0
- Real Node child-process tests ran
- Software-only R4.0 rehearsal: 12/12 PASS
- Hardware was not run

Phase status entering this directive:

- **Phase 0 software gate: ACCEPTED, FROZEN**
- **Phase 0 physical gate: PENDING**
- **Phase 1 observability: COMPLETE FOR R4.0**
- **Ready for supervised R4.0: YES, after the narrow preflight corrections below**
- **Phase 2: conditionally authorized only after R4.0 passes**
- **Phase 3: BLOCKED**

Do not reopen Phase 0 architecture. Fix the concrete production-path misses, run the supervised R4.0 smoke, and
then proceed directly into a practical Phase 2 model benchmark if the smoke gate passes.

## Communication contract

1. Communicate only through `agent_results.md`.
2. Update `agent_results.md` first with this baseline, review disposition, and the exact preflight defects below.
3. Do not create a new architecture phase for these fixes. They are preflight corrections.
4. Do not run R4.10 in this directive.
5. Do not begin personality work.
6. Do not permanently change the production model configuration based on benchmark results without explicit
   operator approval.
7. A failed physical test stops the wave immediately. Fix only the observed failure; do not proceed to Phase 2.

---

# 1. Review disposition

The Phase 1 implementation is accepted. The following findings are real but do not downgrade Phase 1:

## 1.1 Manual motion drops the ticket ID

`/api/control` admits a valid motion ticket but calls `LINK.drive()` / `LINK.move()` with only epoch and generation.
The Air 2 native link correctly rejects missing `ticket_id`, so manual UI motion fails closed on the real robot.

Fix every direct production motion call site to pass the complete admitted ticket:

- epoch
- generation
- ticket ID

Extend the static authority audit to fail when any production call to `drive()` or `move()` supplies a partial
motion ticket.

Add an API contract test proving manual Air 2 motion reaches the fake sidecar with all ticket fields.

## 1.2 Descending cursor pagination is incorrect

The journal currently applies `seq > cursor` regardless of order. That works for ascending pagination but causes
descending pages to repeat newer rows instead of walking backward.

Fix cursor semantics for both directions:

- ascending page after cursor: `seq > cursor_seq`
- descending page after cursor: `seq < cursor_seq`

The opaque cursor must validate its process session or durable sequence domain rather than silently discarding the
session component.

Add tests for:

- ascending pagination across at least three pages
- descending pagination across at least three pages
- no duplicates
- no omissions
- malformed cursor returns HTTP 400
- stale/foreign process-session cursor behavior is explicit and tested

## 1.3 Recovery does not reliably restore the newest bounded tail

The current recovery streams each file from its beginning and stops at the global event budget. A large active
file can therefore restore its oldest rows rather than its newest rows, and persistent queries can miss rotated
history after the scan cap is consumed.

Implement bounded newest-tail recovery correctly:

- process retained files in true chronological order,
- recover the newest `recover_max_events`, not the first records encountered,
- preserve durable sequence ordering,
- scan persistent queries according to the requested time/order window,
- do not let one active file starve all rotated files from consideration.

Use bounded reverse-line reading, indexed file metadata, or another deterministic approach. Do not load all
retained files into memory.

## 1.4 Writer shutdown can strand the writer thread

If the queue is full, `put_nowait(None)` can fail and no shutdown sentinel reaches the writer. The join then times
out, the file can be closed while the daemon writer remains alive, and `_closing` is set only after the fact.

Fix shutdown so:

- the writer always observes closure,
- it drains until the deadline,
- it exits even if no sentinel can be queued,
- the file is never closed while the writer can still write,
- undrained counts are exact,
- repeated close is idempotent.

Add deterministic full-queue shutdown tests.

## 1.5 Loopback trust is unsafe behind a reverse proxy

Trusting `request.client.host` as loopback can expose observability through a local reverse proxy, because remote
requests may appear to originate from `127.0.0.1`.

Use one of these explicit policies:

- owner token required whenever the configured bind is non-loopback, regardless of immediate peer address; or
- trusted-proxy configuration with strict forwarded-client validation.

Default fail-closed. Do not trust arbitrary `X-Forwarded-For` or `Forwarded` headers.

## 1.6 The hardware harness is not yet an honest R4.0 gate runner

The current harness:

- depends on the broken manual motion path,
- records a combined operator question instead of separate halt and post-STOP-motion observations,
- does not include its full `acceptance_report()` in the saved summary,
- relies on operator setup prompts for concurrency/interruption scenarios rather than deterministic scenario
  control,
- does not require an explicit arming ceremony before motion.

Correct these before physical execution.

## 1.7 Acceptance documentation contains stale software counts

Update `docs/PHASE0_ACCEPTANCE.md` so its canonical software evidence references the current immutable evidence
rather than the old 162-pass snapshot. Preserve historical evidence as history, not as the current gate result.

---

# 2. Preflight software gate

Before hardware, implement all corrections in section 1 and run:

```powershell
python -X faulthandler -m pytest -q -p no:recording `
  tests/test_authority_audit.py `
  tests/test_api_contract.py `
  tests/test_observability.py `
  tests/test_observability_api.py `
  tests/test_hardware_harness.py `
  tests/test_rtm_node.py `
  tests/test_sidecar_protocol.py

python scripts/phase1_rehearsal.py

python -X faulthandler -m pytest -q -p no:recording

Set-Location webui
npm ci
npm run build
Set-Location ..
```

One full-suite run is sufficient for this narrow preflight.

Commit the preflight code and evidence before any physical run. The hardware run must reference that exact clean
commit SHA.

---

# 3. Build an honest supervised R4.0 runner

Evolve `scripts/hardware_smoke.py`; do not create a competing harness.

## 3.1 Explicit arming

Physical commands must require all of:

- `--mode r4_0`
- `--armed`
- an interactive typed phrase such as `I AM PHYSICALLY PRESENT`
- a clean git tree
- exact tested preflight SHA
- running app reports the same software SHA
- live Air 2 native link
- synchronized process/sidecar control state
- journal writer healthy
- operator confirmation of the physical safety checklist

`--auto` remains diagnostics-only and can never pass.

The harness must not issue motion before all arming conditions pass.

## 3.2 Physical safety checklist

Require explicit operator confirmation that:

- the robot is on a flat indoor floor,
- at least a 2 meter clear radius exists,
- stairs, ledges, water, cables, pets, and children are excluded,
- the operator is within immediate reach,
- the STOP control is open and visible,
- the operator has a direct physical intervention path,
- battery is sufficient and telemetry/video are live.

Record each confirmation in the evidence manifest.

## 3.3 Conservative physical limits

For R4.0 only:

- cap forward command magnitude at 0.20,
- cap turn magnitude at 0.18,
- cap individual motion duration at 0.60 seconds,
- require a successful normal stop after every ordinary motion trial,
- require explicit reconciled RESUME after every master STOP,
- refuse commands when camera or telemetry freshness exceeds the configured gate,
- never run an unbounded held drive.

These caps are harness-level limits in addition to the frozen safety floor.

## 3.4 Deterministic scenario control

The harness must create the scenario rather than merely ask the operator to create it.

Required R4.0 trials:

- 5 eye commands
- 5 forward pulses
- 5 turn pulses
- 5 normal stops
- 10 master STOP trials, two each:
  1. during an active forward pulse
  2. during an active turn pulse
  3. during an ActionExecutor-controlled move
  4. with multiple motion requests queued/in flight
  5. during a controlled RTM/sidecar interruption

Use existing production paths. A narrowly gated hardware-test scenario endpoint may be added only when:

- it is disabled by default,
- requires loopback plus owner token,
- requires an active armed hardware-gate session,
- exposes only predefined capped scenarios,
- cannot send raw IDs or arbitrary motion,
- expires automatically,
- is covered by authority-audit tests.

Prefer direct harness orchestration through existing APIs where possible.

## 3.5 Separate physical observations

For every physical motion/STOP trial record separate tri-state observations:

- `motion_started_observed`
- `halt_observed`
- `post_stop_motion_observed`
- `unexpected_motion_observed`
- `operator_uncertain`

Unknown is not pass.

For master STOP, require:

- local inhibit asserted,
- priority transport dispatched,
- robot halt observed,
- no post-STOP motion observed,
- latch remains asserted,
- stale effects rejected,
- explicit RESUME required before later motion.

## 3.6 Evidence and thresholds

Save one immutable evidence directory tied to the exact tested SHA:

`data/test-evidence/hardware/<sha>/r4_0/<timestamp>/`

Include:

- manifest and operator checklist
- raw API request/response rows
- canonical journal incident IDs
- incident exports for every master STOP
- command/transport/effect timestamps
- operator observations
- readiness before/after each trial
- frontend/software provenance
- `acceptance_report()` with all gates
- abort reason when applicable

The saved summary must include:

- every required trial count
- STOP p95
- normal command acknowledgment p95
- motion dispatch p95
- every STOP observed halt
- no post-STOP motion
- stale effect rejection
- no unexpected motion
- journal health stayed valid
- clean tree and exact SHA match

Any missing measurement fails the gate.

## 3.7 Abort behavior

Immediately issue priority E-STOP and abort on:

- motion before arming
- unexpected motion
- failure to halt
- post-STOP motion
- stale command accepted
- process/sidecar desynchronization
- loss of camera/telemetry beyond the gate
- failed or ambiguous RESUME
- journal incident missing required STOP evidence
- software SHA mismatch

Do not continue to collect more evidence after a safety failure. One clean failure is more useful than nine
additional opportunities to hit furniture.

---

# 4. Execute the supervised R4.0 gate

This directive authorizes **only** the R4.0 smoke gate, not R4.10.

The harness may be launched only while a human operator is physically present and completes the interactive
arming ceremony. It must never auto-arm or infer presence.

Recommended command after preflight passes:

```powershell
python scripts/hardware_smoke.py --mode r4_0 --armed --base http://127.0.0.1:8200
```

The human operator answers every physical observation prompt.

## R4.0 verdict

PASS only when all required trials and thresholds pass with complete evidence.

On PASS, record:

- **Phase 0 physical gate: R4.0 SMOKE PASSED; R4.10 PENDING**
- **Phase 1 observability: COMPLETE**
- **Phase 2 cognition/model benchmarking: AUTHORIZED**

On FAIL or ABORT, record:

- **Phase 0 physical gate: R4.0 FAILED or ABORTED**
- exact failed invariant/scenario
- hardware left latched and inhibited
- **Phase 2 remains BLOCKED**

Fix only the observed issue and stop. Do not perform broad refactoring after a physical failure.

---

# 5. Conditional Phase 2: cognition benchmark foundation

Proceed with this section only if R4.0 passes in this wave.

Physical motion remains disabled during all Phase 2 benchmarking. Model benchmarking is software/replay based.

## 5.1 Benchmark the real FreeBo workload

Build a reproducible benchmark suite around recorded, redacted, hardware-free scenarios representing:

- simple conversational response
- rapid STOP/interruption recognition
- tool selection and exact structured arguments
- refusal to act when a faculty or safety gate denies action
- multi-step task planning
- vision grounding from stored test frames
- temporal reasoning over short stored frame sequences
- provider timeout/cancellation
- stale result suppression
- recovery after tool/provider failure
- memory retrieval relevance without leaking unrelated memory
- response style/personality separation from safety/tool decisions

No live robot commands are permitted from benchmark cases.

## 5.2 Evaluate model roles, not one mythical universal brain

Benchmark candidates by role:

- fast conversational/router model
- deliberate reasoning/tool model
- vision/omni model
- local fallback model

Support single-model and routed combinations.

Do not hard-code vendor marketing assumptions. Enumerate only providers/models actually available in the current
configuration at runtime, plus explicitly configured local endpoints.

## 5.3 Required metrics

Record at minimum:

- first-token latency
- first useful response latency
- total latency
- tool-call validity
- tool selection accuracy
- argument accuracy
- safety-gate compliance
- interruption cancellation latency
- stale-result rate
- vision grounding score
- task completion score
- retry/recovery success
- input/output tokens when available
- estimated cost when available
- peak local VRAM/RAM when measurable
- provider errors/timeouts

Keep raw benchmark prompts/outputs out of git when they contain personal data. Commit redacted cases, schemas,
scorers, and aggregate results.

## 5.4 Deterministic scoring

Use machine-checkable scoring wherever possible:

- exact JSON schema validation
- expected/forbidden tool sets
- numeric tolerance for arguments
- required safety refusal
- event-trace assertions for cancellation
- labeled visual fixtures

Use human grading only for genuinely subjective conversation quality, and keep it separate from the objective
score.

## 5.5 Benchmark runner and UI

Provide:

- CLI benchmark runner
- resumable result store
- per-model and per-role summaries
- failure-case replay
- side-by-side latency/quality/cost table
- a read-only benchmark panel in the existing UI

The benchmark runner must not alter production model settings.

## 5.6 Recommendation output

Produce a recommendation with:

- best fast interactive model
- best tool/reasoning model
- best vision model
- best local fallback
- best routed stack
- latency/quality/cost tradeoffs
- known failure modes
- configuration changes proposed but not applied

Do not declare a winner from fewer than three runs per deterministic case.

---

# 6. Tests and evidence

Add focused tests for:

- complete manual ticket propagation
- static audit of all motion call sites
- ascending and descending cursor pagination
- cursor session validation
- newest-tail recovery across active and rotated files
- full-queue deterministic writer shutdown
- reverse-proxy access policy
- hardware arming refusal paths
- no motion before arming
- deterministic scenario setup
- physical evidence schema and missing-measurement failure
- abort-on-unexpected-motion behavior
- SHA/tree/readiness gate enforcement
- benchmark schema validation
- scorer correctness
- safety-refusal benchmark cases
- cancellation trace scoring
- benchmark runner cannot touch production settings or robot link

After all code changes and after the physical run, run one final software suite and frontend build. Do not rerun
physical trials merely to improve cosmetic evidence.

---

# `agent_results.md` required final sections

```markdown
# Agent Results

## Directive
## Baseline SHA
## Review disposition
## Preflight defects corrected
## Preflight tested code SHA
## Preflight exact tests and exit codes
## R4.0 arming and operator checklist
## R4.0 physical evidence SHA
## R4.0 trial results
## R4.0 acceptance report
## R4.0 verdict
## Phase status
## Conditional Phase 2 status
## Benchmark architecture
## Benchmark candidates actually tested
## Benchmark results
## Recommended model stack
## Proposed configuration changes
## Exact final tests and exit codes
## Frontend build evidence
## Machine-readable evidence paths
## Known limitations
## Working-tree status
```

Rules:

- Never claim hardware PASS without operator observations.
- Never infer physical effect from SDK success.
- List every aborted or repeated trial.
- Preserve failed evidence; do not overwrite it.
- State exact hardware model/variant and software SHA.
- State whether Phase 2 was entered and why.
- Do not silently apply model recommendations.
- Do not run R4.10.

## Stop point

Stop when one of these occurs:

### Successful path

1. preflight corrections pass,
2. supervised R4.0 passes with complete evidence,
3. Phase 2 benchmark foundation is implemented,
4. configured candidate models are benchmarked with at least three deterministic runs per case,
5. a model-stack recommendation is reported without applying it.

### Failure path

1. preflight cannot pass, or
2. R4.0 fails/aborts, or
3. operator is not physically present.

On the failure path, leave the robot latched/inhibited, preserve evidence, report, and stop.
