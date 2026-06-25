# Agent Next 3: Close the Final Drive-Ticket Gap, Then Begin Phase 1

This directive intentionally ends the endless Phase 0 expansion cycle.

The repository has completed the large Phase 0 software-hardening wave and has three clean full-suite runs at
`b445a0dca436afaddf8e6b58abaee82fe50ccefa` with 210 passed and only the two hardware-gated tests skipped.
The current report commit is `09d2da9ad41158a5c25235e5bc56a620fa733b9b`.

One concrete authority defect remains before Phase 1 work begins: the Node sidecar's `drive` handler still uses
bespoke partial validation instead of the mandatory effect-ticket validator used by the other physical effects.
Fix that defect, verify it, freeze the Phase 0 software architecture, and then begin Phase 1 observability.

## Communication contract

1. Communicate only through `agent_results.md`.
2. Update `agent_results.md` first with this directive, the current baseline, and the exact remaining defect.
3. Do not stop after writing a plan. Implement the work below in small reviewable commits.
4. Do not run the live robot, hardware smoke tests, movement calibration, R4.0, or R4.10.
5. Do not reopen Phase 0 architecture unless a new test proves a regression in a safety invariant.
6. Phase 0 physical acceptance remains pending. Phase 2 cognition/model benchmarking and Phase 3 personality
   remain blocked until the R4.0 physical smoke gate passes.
7. Phase 1 observability is authorized only after Gate A below is green.

---

# Gate A: Final Phase 0 software correction

## A1. Make sidecar `drive` use the same mandatory ticket validator

The current sidecar `drive` branch does not call `effectOk(c)`. It currently requires generation, while epoch and
sidecar identity are optional, and it does not require process identity or `ticket_id`.

Replace that bespoke admission logic with the same strict authority contract used by other physical effects.

A non-zero drive command must require and validate all of:

- `process_instance_id`
- `sidecar_instance_id`
- `epoch`
- `generation`
- `ticket_id`
- motion effect class, either explicit `effect_class="motion"` or an equivalent command-specific invariant

Validation must reject:

- missing process identity
- missing sidecar identity
- missing epoch
- missing generation
- missing ticket ID
- wrong process instance
- wrong sidecar instance
- stale epoch
- stale generation
- latched state
- STOP in flight
- an effect class other than motion, when an explicit class is carried

Zero/deadman motion and E-STOP remain exempt from ordinary effect admission.

## A2. Bind the active drive repeat to the full ticket

When the initial drive frame succeeds, capture the full accepted authority tuple:

- process instance ID
- sidecar instance ID
- epoch
- generation
- ticket ID

Every repeat iteration must stop and clear the drive immediately when any captured value no longer matches the
current accepted state, or when the latch/STOP state changes.

Do not validate only generation. A same-generation/new-epoch transition must kill the repeat.

The delayed stop timer must also belong to the same drive instance. A stale timer from an older drive must not
clear or interfere with a newer drive stream.

## A3. Queue invalidation

The serialized sidecar queue must reject a stale queued drive using the full ticket authority, not only a quick
generation comparison.

Prefer one shared drive/effect validator rather than duplicating admission logic in the queue and command
handler. The command handler remains the final authority even if the queue performs an early rejection.

## A4. Strengthen the static audit

Extend `tests/test_authority_audit.py` so it fails when the sidecar `drive` branch stops using the mandatory
validator or allows missing identity/ticket fields again.

The audit should not merely search Python for removal of `_ticketed`. It must cover the JavaScript drive authority
contract as well.

## A5. Required deterministic tests

Add or update real Node child-process tests for all of these:

1. Drive with no identity is rejected.
2. Drive with identity but no ticket ID is rejected.
3. Drive with ticket ID but no epoch is rejected.
4. Drive with the complete current ticket succeeds.
5. Drive with correct generation but stale epoch is rejected.
6. Drive with wrong process ID is rejected.
7. Drive with wrong sidecar ID is rejected.
8. Drive carrying a non-motion effect class is rejected, if the class is explicit.
9. An active repeating drive is terminated when a newer epoch is installed, even if generation is unchanged in
   the test seam.
10. An active repeating drive is terminated immediately by STOP.
11. A queued drive whose ticket becomes stale before dequeue never reaches the fake SDK send.
12. A delayed stop timer from drive A cannot terminate a newer drive B.

Use barriers/test-seam controls for repeat and queue races. Do not rely on arbitrary sleeps as the proof.

## A6. Verification

Run:

```powershell
python -X faulthandler -m pytest -q -p no:recording `
  tests/test_sidecar_protocol.py `
  tests/test_adversarial_integration.py `
  tests/test_authority_audit.py `
  tests/test_priority_stop.py `
  tests/test_rtm_node.py

python -X faulthandler -m pytest -q -p no:recording
```

One clean full-suite run is sufficient for this narrow correction. Record exact counts and exit codes in
`agent_results.md`.

## A7. Gate result

When Gate A is green, record these statuses exactly:

- **Phase 0 software gate: ACCEPTED FOR PHASE 1**
- **Phase 0 physical gate: PENDING, HARDWARE NOT RUN**
- **Phase 1 observability: AUTHORIZED**
- **Phase 2 cognition/model benchmarking: BLOCKED pending R4.0 physical smoke**
- **Phase 3 personality: BLOCKED pending Phases 1 and 2**

Do not describe Phase 0 as fully passed. Software acceptance for Phase 1 and physical acceptance are separate.

---

# Gate B: Update the phase contract

Update `docs/ROADMAP.md`, `docs/CURRENT_STATE.md`, and the relevant Phase 0 acceptance documentation so the phase
contract matches the status above.

The roadmap must no longer say that every part of physical Phase 0 acceptance must finish before any Phase 1
observability work can begin.

Use this policy:

- Phase 1 may proceed after the Phase 0 software safety architecture is accepted.
- Physical movement remains disabled by policy until a supervised hardware gate is explicitly authorized.
- Phase 1 exists partly to improve evidence and incident diagnosis before and during physical acceptance.
- Phase 2 and Phase 3 remain blocked until the initial physical safety smoke gate succeeds.

Add a frozen Phase 0 software invariant list. At minimum:

- all physical effects require current authority tickets
- priority STOP is exactly-once per source and never delayed behind normal action cancellation
- RESET is prepared and committed, fail-closed
- process and sidecar identities are exact
- STOP invalidates reasoning and all stale effects
- operator telemetry/video may survive STOP; autonomous faculties do not
- no production raw physical-effect bypass

Future changes may extend these invariants but may not weaken them without an explicit reviewed migration.

---

# Gate C: Begin Phase 1 observability

Do not turn Phase 1 into another abstract planning document. Deliver a usable first vertical slice.

## C1. Unified structured event schema

Create one append-only structured event model for safety, faculty, reasoning, transport, and motion events.

Every event must include:

- event ID
- monotonic timestamp
- wall-clock UTC timestamp
- process instance ID when available
- sidecar instance ID when available
- transition epoch and generation when relevant
- correlation/command/action/ticket IDs when relevant
- category
- event type
- source
- requested state or action
- effective decision
- reason/denial reason
- outcome
- latency fields when relevant
- a bounded structured detail object

Define explicit categories for at least:

- `safety.transition`
- `safety.faculty_decision`
- `control.effect_admission`
- `control.transport`
- `reason.lifecycle`
- `reason.tool`
- `speech.lifecycle`
- `vision.lifecycle`
- `motion.lifecycle`
- `system.lifecycle`

Do not log secrets, cloud tokens, API keys, full audio, or raw image data.

## C2. Event journal

Implement a bounded in-memory journal plus durable JSONL persistence.

Requirements:

- non-blocking or minimally blocking writes
- bounded queue/backpressure policy
- rotation by size and/or date
- retention limit
- malformed-event protection
- redaction at the journal boundary
- deterministic shutdown flush
- startup recovery that tolerates a truncated final JSONL line

The robot must remain safe if persistence fails. Journal failure is surfaced but does not bypass STOP or block a
priority zero send.

## C3. Instrument the important paths

Emit structured events for:

- every STOP source, local latch, priority dispatch, initial zero-send result, retry, and completion
- RESET admission, prepare, commit, process finalization, rejection, and relatch degradation
- every faculty requested/effective state change and denial
- every effect admission, stale-ticket rejection, dispatch, acknowledgment, and outcome
- reason cycle queued, started, cancelled, completed, and failed
- provider request start/end/cancel/failure without recording private prompt contents by default
- tool requested/admitted/denied/completed
- motion action lifecycle and evidence classification
- sidecar spawn, bind, connect, disconnect, replacement, and shutdown
- speech render/play/cancel lifecycle
- AI vision/caption request/cancel/discard lifecycle

Reuse existing event/metrics surfaces where sensible, but establish one canonical structured record rather than
several incompatible dictionaries.

## C4. Query API

Add an authenticated/local-owner API for querying the event journal.

At minimum support:

- time range
- category
- event type
- source
- outcome
- correlation ID
- transition epoch/generation
- limit and cursor pagination

Add endpoints for:

- recent timeline
- one correlation trace
- one STOP/RESUME incident trace
- summary counts and latency percentiles over a selected time range

Responses must be bounded. Never load an unbounded journal into memory for one request.

## C5. Timeline inspector UI

Add a first useful timeline/inspector page to the existing web UI.

It must show:

- chronological event rows
- category/type/source/outcome
- requested versus effective faculty state
- STOP/RESUME transitions grouped by correlation
- effect ticket epoch/generation
- transport acknowledgment and latency
- expandable structured details
- filters matching the query API
- a clear marker for denied, cancelled, degraded, and failed events

No elaborate visual-design detour. A readable engineering inspector beats an animated dashboard shaped like a
spaceship.

## C6. Exportable incident bundle

Add a safe incident export for a selected time window or correlation ID.

The bundle must include:

- redacted JSONL events
- current software SHA
- platform/version summary
- relevant readiness snapshot
- metric summary
- no credentials, tokens, audio payloads, images, or personal memory contents

Make the export deterministic and unit-tested.

## C7. Tests

Add tests for:

- schema validation
- redaction
- bounded queue behavior
- persistence failure behavior
- rotation and retention
- truncated-line recovery
- query filtering and pagination
- correlation trace assembly
- STOP/RESUME incident grouping
- export redaction and manifest correctness
- UI API-client failure states
- journal shutdown flush

Run the full suite after the Phase 1 slice. Do not run hardware.

---

# `agent_results.md` required final format

Use these top-level sections:

```markdown
# Agent Results

## Directive
## Baseline SHA
## Gate A tested code SHA
## Phase gate status
## Final drive-ticket correction
## Gate A exact tests and exit codes
## Phase contract changes
## Phase 1 tested code SHA
## Observability architecture
## Instrumented paths
## Query API
## Timeline inspector
## Incident export
## Phase 1 exact tests and exit codes
## Known limitations
## Hardware status
## Working-tree status
```

Rules:

- Keep hardware status `NOT RUN`.
- Do not call Phase 0 fully passed.
- Clearly distinguish the SHA tested for Gate A from the SHA tested for the Phase 1 slice.
- List every skipped test.
- Record whether the real Node child-process tests ran.
- Do not report observability events that are not actually wired into runtime paths.
- Stop after the first usable Phase 1 observability slice and complete report.

## Stop point

Stop when:

1. the drive handler enforces the complete ticket contract,
2. the focused tests and one full suite pass,
3. the Phase 0 software gate is marked accepted for Phase 1,
4. the physical gate remains pending,
5. the first end-to-end observability slice is implemented and tested,
6. `agent_results.md` contains the complete report.

Do not run hardware. Do not begin Phase 2 or Phase 3.
