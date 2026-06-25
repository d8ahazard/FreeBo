# Agent Next 4: Complete Observability and Prepare the Supervised Hardware Gate

This directive prioritizes forward progress.

The seven-commit `agent_next_3` wave is accepted as a successful transition into Phase 1:

- Gate A drive-ticket correction tested at `e7acf6d6107b3c3c443e7da8196095022a76875d`.
- Phase 1 first slice tested at `eaa7dbeb6e787bf80a3d17e1b0633f272a1e4db7`.
- Final report commit: `60f3edf2c75cd680ff525f544237f441efe99bc0`.
- Reported suite: 231 passed, 2 intentionally hardware-gated skips, exit 0.
- Real Node child-process tests ran.
- Hardware was not run.

Do not reopen Phase 0 architecture. The Phase 0 software gate remains **ACCEPTED FOR PHASE 1** and its frozen
invariants remain frozen. Minor defects found during this review belong in the Phase 1 work below unless they
prove an actual safety regression.

The goal of this wave is to finish a practical Phase 1 observability system and leave FreeBo ready for a
separately authorized, supervised R4.0 hardware smoke run. This directive still does **not** authorize hardware.

## Communication contract

1. Communicate only through `agent_results.md`.
2. Update the report first with this baseline and the review corrections below.
3. Implement the work in a small number of cohesive commits. Do not create a commit for every paragraph.
4. Do not run the live robot, movement calibration, R4.0, R4.10, or any hardware command.
5. Do not begin Phase 2 model benchmarking or Phase 3 personality work.
6. Do not expand the Phase 0 safety protocol unless a deterministic test proves an invariant is broken.
7. Roll small fixes into the relevant Phase 1 implementation instead of pausing for separate mini-phases.

---

# 1. Accept the current work and correct the scope of its claims

Keep the current drive-ticket implementation, frozen invariants, event schema, timeline, query API, and incident
export. They are valid foundations.

Update `agent_results.md` to record these review findings without treating them as release blockers:

1. The event API is currently same-origin, but it is not meaningfully authenticated merely because the browser
   calls it from the same origin.
2. STOP and RESUME currently use different correlation IDs (`stop-genN` and `reset-genN`), so the API cannot yet
   provide one complete STOP→RESET incident trace.
3. The durable JSONL survives a process restart, but the query surface only sees the current in-memory ring. The
   journal does not hydrate retained history or continue its cursor sequence on startup.
4. Persistence currently writes and flushes synchronously under the journal lock. This is acceptable for the
   first slice, but it is not a real non-blocking writer or backpressure implementation.
5. Redaction is strongest on `detail`; every free-text envelope field is not yet normalized through the same
   boundary policy.
6. Time-range filtering was requested but is not present yet.
7. Reason, tool, transport, speech, vision, and detailed sidecar lifecycle categories exist in the schema but are
   not wired into runtime paths yet.
8. The Timeline React fragment lacks its own key and the UI refetches a full result window every three seconds.
   These are minor implementation issues. Fix them while building the live timeline.

Do not downgrade the accepted phase status because of these items. Fix them as Phase 1 continuation work.

---

# 2. Journal v2: durable, restart-aware, and non-blocking

Evolve `autobot/observability.py`; do not build a second competing logging system.

## 2.1 Process session and cursor identity

Add a generated `process_session_id` to every event.

A cursor must remain unambiguous across process restarts. Use either:

- an opaque cursor encoding `{process_session_id, seq}`, or
- a durable monotonically increasing sequence recovered from retained journal state.

Do not return a bare integer cursor that silently collides after restart.

Keep globally unique event IDs.

## 2.2 Bounded startup recovery

At startup:

- discover the active JSONL and retained rotated files,
- stream records rather than calling `readlines()` on an unbounded file,
- tolerate malformed and truncated lines,
- restore the newest bounded history into the in-memory query ring,
- restore the next sequence/cursor state,
- record a recovery summary event with files scanned, valid events restored, malformed rows skipped, and the
  oldest/newest available timestamps.

Recovery must be bounded by configured file count, byte limits, and maximum recovered events.

## 2.3 Background persistence writer

Move durable writes off safety and event-loop call paths.

Required behavior:

- synchronous creation/redaction and insertion into the bounded in-memory ring,
- non-blocking enqueue to a bounded writer queue,
- one tracked writer thread or task,
- batch writes and periodic flush rather than flushing every event,
- deterministic drain with a bounded shutdown deadline,
- rotation and retention owned only by the writer,
- explicit counters for enqueued, persisted, queue-dropped, persistence-failed, and shutdown-undrained events.

When the persistence queue is full:

- never block STOP, RESET, faculty gating, or control dispatch,
- preserve the event in the in-memory ring,
- increment a loss counter,
- surface a journal-health event when capacity recovers, without recursively flooding the queue.

Add a priority flag only if necessary. Do not create an unbounded emergency queue under the banner of safety.

## 2.4 Complete boundary normalization and redaction

Apply bounded normalization/redaction to every untrusted free-text or structured field, including:

- `type`
- `source`
- `requested`
- `effective`
- `reason`
- `outcome`
- `correlation_id`
- `command_id`
- `detail`

Preserve useful identifiers while masking credentials, tokens, prompts, audio/image payloads, personal memory
content, and excessively long text.

Do not log spoken text, transcripts, model prompts, full model responses, images, audio, or memory contents.
Useful safe substitutes include byte count, character count, duration, model/provider label, result class, and a
short non-reversible correlation hash when truly needed.

## 2.5 Journal health

Expose journal health in `/api/status` and the observability summary:

- writer alive
- queue depth/capacity
- persisted count
- queue-dropped count
- persistence failures
- recovered event count
- active file size
- retained file count
- oldest/newest queryable timestamps

Journal failure must remain diagnostic. It must not weaken or block the safety system.

---

# 3. One causal incident model

Add first-class causal fields to the event envelope:

- `incident_id`
- `parent_event_id`
- `phase`

Keep `correlation_id` for operation-level grouping, but use `incident_id` for the larger causal story.

## 3.1 STOP→RESET incident

A master STOP creates one incident ID. Carry it through:

- process inhibit/latch
- executor cancellation
- sidecar priority dispatch
- initial zero send
- retries
- transport result
- faculty inhibition
- RESET admission
- prepare
- commit
- process finalization
- relatch/degraded recovery, when applicable
- successful RESUME

A single incident query must return the whole STOP→RESET lifecycle in chronological order.

Do not attempt to derive this only from generation arithmetic in the UI. Carry the incident explicitly through
the runtime transition context.

## 3.2 Other causal traces

Use incident/correlation relationships for:

- one reason cycle and its provider/tool/effect children,
- one motion action and its ticket/transport/evidence children,
- one speech request through render/play/cancel,
- one vision request through capture/model/result/discard,
- one sidecar process lifetime through bind/connect/reconnect/shutdown.

No distributed-tracing framework is required. A few explicit IDs are enough. Humanity has already suffered
sufficiently from telemetry frameworks that need their own telemetry frameworks.

---

# 4. Complete runtime instrumentation

Wire the categories that currently exist only on paper.

## 4.1 `reason.lifecycle`

Emit at least:

- queued
- lock_wait_started
- started
- provider_wait_started
- cancelled
- completed
- failed

Include trigger, safe brain mode/provider/model label, queue/lock latency, execution latency, cancellation reason,
and incident/correlation IDs. Do not record prompts or model output text.

Ensure a cancelled stale cycle cannot emit a later `completed` event.

## 4.2 `reason.tool`

Emit:

- requested
- admitted or denied
- started
- completed
- failed or cancelled

Include safe tool name, argument keys only or a tool-specific sanitized summary, result class, latency, and any
child effect ticket/action ID. Never persist arbitrary tool arguments or result payloads wholesale.

## 4.3 `control.transport`

Instrument the Python-to-sidecar and sidecar-to-Agora lifecycle:

- queued_to_sidecar
- sidecar_received
- validation_denied
- sdk_send_started
- sdk_send_succeeded or failed
- acknowledgement_received
- timed_out
- retry_started/completed for E-STOP

Carry command ID, RTM message ID, process/sidecar IDs, ticket ID, epoch/generation, incident/correlation IDs, and
latency stages.

Sidecar events must enter the canonical journal through `RtmNode`; do not create a second JavaScript log file.

## 4.4 `speech.lifecycle`

Instrument:

- requested
- render_started/completed/failed
- publish_started/completed/failed
- playback_started/completed
- cancelled
- stale_result_discarded

Record character count, audio duration/byte count, engine label, playback ID, latency, and reason. Do not record
text or audio.

Cover brain speech, manual/overseer speech, and barge-in cancellation through the shared service.

## 4.5 `vision.lifecycle`

Instrument AI-facing vision only, not every operator preview frame:

- frame_selected
- caption/VLM/omni request_started
- completed
- failed
- cancelled
- stale_result_discarded

Record frame sequence, frame age, dimensions, model label, latency, and result class. Do not record pixels,
base64, caption text, or prompts.

## 4.6 Sidecar and system lifecycle

Record:

- sidecar spawn
- ready/bound
- RTM connect attempt/result
- synchronized or unsynchronized transition
- session refresh
- disconnect
- replacement
- parent-death fail-safe
- shutdown result

These should make a transport outage diagnosable without reading free-form stderr logs.

---

# 5. Persistent query API and access policy

## 5.1 Query semantics

Extend the query API with:

- UTC start/end time
- incident ID
- process session ID
- command ID
- ticket ID
- event ID
- existing category/type/source/outcome/epoch/generation filters
- opaque cursor pagination
- ascending or descending order

Queries must cover the retained persistent window, not only events emitted since the latest process start.

Keep results bounded. Stream or incrementally scan retained JSONL files; do not load all retained history for one
request.

## 5.2 Access policy

Do not describe the API as authenticated unless it actually is.

Implement this practical policy:

- loopback-only deployments may access the inspector with the existing local UI session,
- when the server binds to a non-loopback address, observability query/export endpoints require a configured
  owner token or the project's existing authenticated owner mechanism,
- exports use the same authorization,
- never place the token in query strings,
- return 401/403 rather than a JSON success envelope on denial.

Do not build a general identity platform in this phase. Protect the sensitive diagnostic surface proportionally.

## 5.3 Incident and readiness endpoints

Add explicit endpoints for:

- one incident by `incident_id`
- incidents list with start/end/outcome/severity
- journal health
- hardware-gate readiness report

The hardware-gate readiness report is software-only and must state `hardware_run=false`.

---

# 6. Live timeline and incident inspector

Upgrade the first Timeline tab rather than replacing it.

## 6.1 Live delivery

Deliver canonical events to the browser through either:

- the existing WebSocket event channel with a structured `journal_event` envelope, or
- a dedicated SSE endpoint.

Use cursor catch-up on reconnect, then stream new events. Remove full-window polling every three seconds.

Handle:

- reconnect
- missed-event catch-up
- duplicate suppression by event ID
- bounded client memory
- paused live mode while inspecting history

## 6.2 Inspector experience

Add:

- UTC/local timestamp toggle
- category/type/outcome/source filters
- time-range filter
- incident list and incident detail view
- reason cycle trace
- motion trace
- transport stage latency view
- journal health indicator
- copy event ID/correlation/incident ID
- export selected incident or time range

Display process/sidecar session, epoch/generation, ticket, command, and parent relationships when present.

Fix the existing fragment key warning while touching the component.

No design-system rewrite. Keep it fast, readable, and useful during a robot test.

---

# 7. Software-only R4.0 rehearsal

Build a deterministic rehearsal that proves the observability system can explain the hardware acceptance flow
before anyone puts the robot on the floor.

Add a script such as:

`python scripts/phase1_rehearsal.py`

It must use the mock link and/or real Node FAKE sidecar only. No cloud session and no hardware.

Exercise at least:

1. startup and sidecar bind/synchronization,
2. admitted motion command through fake transport,
3. master STOP during active repeat,
4. failed initial E-STOP send,
5. successful STOP followed by two-phase RESUME,
6. RESET prepare invalidated by a newer STOP,
7. reason cycle cancelled during provider wait,
8. speech render/play cancelled by STOP,
9. vision request result discarded after See-off or STOP,
10. sidecar replacement/reconnect,
11. journal queue pressure and persistence failure,
12. clean shutdown and restart recovery.

For every scenario, assert that the resulting incident trace contains the required ordered events and terminal
outcome. Produce a redacted incident bundle under `data/test-evidence/software/<tested-sha>/rehearsal/`.

The rehearsal report must explicitly say:

- `hardware_run=false`
- `physical_acceptance=false`
- `ready_for_supervised_R4_0=true|false`
- reasons for any false result

Do not call this R4.0 itself. It is a software rehearsal for R4.0.

---

# 8. Minor fixes to carry with this wave

Fix these without creating a new gate or detour:

1. `EventJournal.configure()` must close/drain an existing configured journal before replacement, including in
   tests.
2. Replace `readlines()` recovery with streaming bounded recovery.
3. Normalize invalid integer/time query parameters to explicit HTTP 400 responses rather than silently ignoring
   them.
4. Return correct non-2xx responses for observability persistence/export failures when the requested result
   cannot be produced.
5. Make incident export use an attachment filename and stable schema version metadata.
6. Avoid invoking `git` subprocesses per export; cache software provenance at startup.
7. Track background server tasks created at startup and cancel/await them during shutdown.
8. Add the React fragment key and abort or serialize overlapping timeline requests.
9. Keep the connect-time assert-latch `set_control` limitation documented. Do not stop Phase 1 to redesign it
   unless testing proves it causes an unsafe or unrecoverable state.

---

# 9. Tests and evidence

Add focused tests for:

- restart recovery across rotated files
- cursor uniqueness across process sessions
- bounded writer queue and drop accounting
- writer shutdown drain/deadline
- persistence failure and recovery health events
- full-envelope redaction
- time-range and persistent queries
- malformed query parameter responses
- access control on non-loopback configuration
- STOP→RESET single-incident trace
- reason/tool/transport/speech/vision lifecycle ordering
- no stale completion after cancellation
- sidecar lifecycle trace
- WebSocket/SSE catch-up and duplicate suppression
- incident export authorization/redaction
- rehearsal scenario trace completeness

Run at minimum:

```powershell
python -X faulthandler -m pytest -q -p no:recording `
  tests/test_observability.py `
  tests/test_observability_api.py `
  tests/test_reason_cancellation.py `
  tests/test_speech.py `
  tests/test_rtm_node.py `
  tests/test_sidecar_protocol.py `
  tests/test_priority_stop.py

python scripts/phase1_rehearsal.py

python -X faulthandler -m pytest -q -p no:recording

Set-Location webui
npm ci
npm run build
Set-Location ..
```

One clean full-suite run is sufficient. Record actual counts, exit codes, environment versions, Node child-test
status, rehearsal result, and frontend hashes.

Do not hard-code an expected test count.

---

# 10. Phase status at completion

When this wave is green, record:

- **Phase 0 software gate: ACCEPTED, FROZEN**
- **Phase 0 physical gate: PENDING, HARDWARE NOT RUN**
- **Phase 1 observability: COMPLETE FOR R4.0**
- **Software rehearsal: PASS or FAIL**
- **Ready for supervised R4.0: YES or NO**
- **Phase 2 cognition/model benchmarking: BLOCKED pending supervised R4.0**
- **Phase 3 personality: BLOCKED**

A `YES` for readiness is not authorization to run hardware. Stop and report.

---

# `agent_results.md` required final sections

```markdown
# Agent Results

## Directive
## Baseline SHA
## Review disposition
## Tested code SHA
## Phase status
## Journal v2
## Causal incident model
## Instrumented runtime paths
## Persistent query and access policy
## Live timeline and inspector
## Software-only R4.0 rehearsal
## Minor fixes rolled forward
## Exact test commands and exit codes
## Frontend build evidence
## Machine-readable evidence paths
## Known limitations
## Hardware status
## Working-tree status
```

Rules:

- Report only categories actually wired into runtime.
- List every skipped test.
- State whether real Node child-process tests ran.
- State whether the rehearsal used mock/FAKE components only.
- Do not claim authentication if only same-origin behavior exists.
- Do not claim persistent history is queryable unless restart tests prove it.
- Do not call the rehearsal a physical acceptance run.
- Keep hardware status `NOT RUN`.

## Stop point

Stop when:

1. the journal is restart-aware and non-blocking,
2. STOP→RESET forms one causal incident,
3. all declared event categories are wired and tested,
4. retained persistent history is queryable with protected access,
5. the live inspector is useful without polling full windows,
6. the software-only R4.0 rehearsal produces complete traces,
7. one full suite and frontend build pass,
8. `agent_results.md` is complete and honest.

Do not run hardware. Do not begin Phase 2 or Phase 3.
