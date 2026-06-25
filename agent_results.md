# Agent Results

> Report for `agent_next_3.md` ("Close the Final Drive-Ticket Gap, Then Begin Phase 1"). Communicate only here;
> every claim tied to a tested SHA + exact command + exit code. Phase 0 SOFTWARE is accepted FOR PHASE 1; the
> Phase 0 PHYSICAL gate stays PENDING and hardware is NOT RUN.

## Directive
`agent_next_3.md`: Gate A — make the Node sidecar `drive` handler use the SAME mandatory effect-ticket validator
as the other physical effects, bind the active-drive repeat + delayed-stop to the full per-drive ticket, harden
queue invalidation, extend the static audit to the JS drive contract, add deterministic Node tests, and freeze
the Phase 0 software architecture. Gate B — update the phase contract docs + a frozen invariant list. Gate C —
deliver the first usable Phase 1 observability slice.

## Baseline SHA
- agent_next_2 tested code: `b445a0dca436afaddf8e6b58abaee82fe50ccefa`; report `09d2da9ad41158a5c25235e5bc56a620fa733b9b`.
- Environment: Node v22.16.0, Python 3.10.11, win32.

## Gate A tested code SHA
`e7acf6d` (drive-ticket correction). The drive defect is fixed: the sidecar `drive` handler now routes through a
shared `driveTicketError()` (= `effectOk` + motion-class check) — `process_instance_id`, `sidecar_instance_id`,
`epoch`, `generation`, `ticket_id` are ALL mandatory (missing rejected, not just stale); an explicit non-motion
class is rejected; latched / STOP-in-flight refused. `ticket_id` is threaded end-to-end (executor →
RobotLink.move/drive → Air2NativeLink → sidecar; all link impls updated).

## Phase gate status
- **Phase 0 software gate: ACCEPTED FOR PHASE 1**
- **Phase 0 physical gate: PENDING, HARDWARE NOT RUN**
- **Phase 1 observability: AUTHORIZED**
- **Phase 2 cognition/model benchmarking: BLOCKED pending R4.0 physical smoke**
- **Phase 3 personality: BLOCKED pending Phases 1 and 2**
(Phase 0 is NOT "fully passed": software acceptance for Phase 1 and physical acceptance are separate.)

## Final drive-ticket correction
- A1: `drive` uses the shared mandatory validator (no bespoke generation-only admission; no optional epoch/
  identity). A2: the active repeat + delayed-stop are bound to the full accepted tuple {process id, sidecar id,
  epoch, generation, ticket id} + a per-drive instance id (`_driveSeq`) — a same-generation/new-epoch transition,
  an instance change, a STOP, or a newer drive kills the stream, and a stale stop timer from an older drive can
  never clear a newer one. A3: the serialized queue rejects stale drives via the SAME validator (shared with the
  handler, which remains the final authority). A4: `tests/test_authority_audit.py` now also enforces the JS drive
  contract (must use `driveTicketError`, must not re-introduce optional identity/ticket or the bespoke
  generation-only admission, queue must not use a bare generation compare).

## Gate A exact tests and exit codes
`python -X faulthandler -m pytest -q -p no:recording ...`; Node v22.16.0 / Python 3.10.11 / win32.
- Focused: `tests/test_sidecar_protocol.py tests/test_adversarial_integration.py tests/test_authority_audit.py
  tests/test_priority_stop.py tests/test_rtm_node.py` → **57 passed**, exit 0.
- Full suite at `e7acf6d`: **216 passed, 2 skipped** (both `--hardware`), exit 0.
- A5 deterministic Node child-process drive tests (all 12; repeat/queue/timer races forced via the
  `__diag`/`__block`/`__pause_pump` test seams, NOT sleeps): admission matrix (no identity / no ticket / no
  epoch / full ticket / stale epoch / wrong process / wrong sidecar / non-motion class) in
  `test_sidecar_protocol.py::test_drive_admission_full_ticket_contract`; repeat-killed-by-newer-epoch,
  repeat-killed-by-STOP, queued-stale-drive-never-sends, stale-timer-cannot-stop-newer-drive in
  `test_adversarial_integration.py`. Real Node child-process tests RAN (Node present).

## Phase contract changes
`docs/ROADMAP.md`, `docs/CURRENT_STATE.md`, `docs/PHASE0_ACCEPTANCE.md` now state the two separate Phase 0
gates (software ACCEPTED FOR PHASE 1; physical PENDING), allow Phase 1 to proceed after software acceptance
(Phase 1 partly exists to improve incident diagnosis before/during physical acceptance), keep Phase 2/3 blocked
pending R4.0, and add the FROZEN Phase 0 software invariant list (tickets on all effects; exactly-once priority
STOP; two-phase fail-closed RESET; exact identities; STOP invalidates reasoning + stale effects; operator
telemetry may survive STOP but faculties don't; no raw physical-effect bypass) — extendable, not weakenable
without a reviewed migration.

## Phase 1 tested code SHA
`eaa7dbe` (observability slice). Distinct from the Gate A SHA `e7acf6d`.

## Observability architecture
`autobot/observability.py`: one canonical append-only `Event` model (10 categories — safety.transition,
safety.faculty_decision, control.effect_admission, control.transport, reason.lifecycle, reason.tool,
speech.lifecycle, vision.lifecycle, motion.lifecycle, system.lifecycle) with the full required envelope (id,
seq, monotonic + UTC ts, process/sidecar id, epoch/generation, correlation/command/ticket id, category, type,
source, requested, effective, reason, outcome, latency, bounded detail). `EventJournal` = bounded in-memory ring
+ durable JSONL with: boundary redaction (secrets/tokens/prompts/audio/image bytes), rotation by size +
retention, FAIL-SAFE persistence (errors surfaced via counter/callback, never raised, never blocking a STOP),
deterministic flush/close, truncated-final-line-tolerant recovery. Configured at server startup, flushed on
shutdown.

## Instrumented paths
WIRED INTO RUNTIME (and only these are claimed): `control.effect_admission` (every `SafetyFloor.admit_effect`
grant/deny); `safety.transition` (every STOP dispatch, correlated `stop-genN`; RESUME, `reset-genN`);
`safety.faculty_decision` (each capability broadcast); `motion.lifecycle` (every terminal `ActionExecutor`
action with verdict + latency, correlated by action id); `system.lifecycle` (startup/shutdown). NOT yet emitted:
reason.lifecycle, reason.tool, control.transport, speech.lifecycle, vision.lifecycle (left for the next slice;
not reported as wired).

## Query API
Authenticated/local (same-origin): `GET /api/events` (filters: category/type/source/outcome/correlation_id/
epoch/generation + `since_seq` cursor + bounded `limit`), `GET /api/events/recent`, `GET /api/events/trace/{cid}`
(one correlation/incident trace), `GET /api/events/summary` (counts by category/outcome + latency p50/p95/max).
Responses are bounded (the in-memory ring is the query surface; no unbounded file load per request).

## Timeline inspector
A "Timeline" tab in the web UI (`webui/src/components/TimelinePanel.tsx`): chronological rows with
category/type/source, requested→effective, epoch/generation, latency, outcome; colour markers for
denied/degraded/failed/cancelled/inhibited; category + correlation-id filters matching the query API; expandable
redacted detail; an export link; 3s refresh. Built with `npm run build` (tsc clean).

## Incident export
`GET /api/events/export` (optional `correlation_id`): a deterministic redacted bundle (`freebo.incident.v1`) —
software SHA, platform/python summary, readiness snapshot, metric summary, and the redacted JSONL events. No
credentials/tokens/audio/images/memory (redaction is enforced at the journal boundary; verified by test).

## Phase 1 exact tests and exit codes
- `tests/test_observability.py` (journal/schema/redaction/bounded-ring/persistence-failure/rotation+retention/
  truncated-recovery/query+pagination/correlation-trace/summary/safe-before-configure) → **10 passed**.
- `tests/test_observability_api.py` (STOP emits correlated safety.transition; effect admission emits;
  query-endpoint filters; correlation trace; export redaction + manifest; summary endpoint) → **5 passed**.
- Full suite at `eaa7dbe`: **231 passed, 2 skipped** (both `--hardware`), exit 0, ~80s. Raw output +
  `summary.json` under `data/test-evidence/software/eaa7dbeb6e78/`. Real Node child-process tests RAN.
- Frontend: `npm ci && npm run build` (tsc clean). Entry `assets/index-D6LAEPyi.js` SHA-256
  `83408B20469910CEABE6D5AC400BAAF5E2159D561454E65DD00CE9CE058001FA` (full hashes in summary.json). `webui/dist`
  is gitignored (built on deploy).

## Known limitations
- HARDWARE NOT RUN. Phase 0 physical gate PENDING; Phase 2/3 BLOCKED pending R4.0.
- Skipped tests: ONLY `tests/test_hardware.py` (2 cases, `--hardware`-gated). No other skips.
- Observability is a FIRST slice: reason/tool/transport/speech/vision categories exist in the schema but are
  NOT yet emitted from runtime (not claimed as wired). The journal write is synchronous-under-lock (minimally
  blocking) rather than a separate writer thread; persistence is fail-safe regardless.
- The §3 connect-time reconcile still uses a fire-and-forget assert-latch `set_control` (cannot unlatch;
  documented in the prior wave) — unchanged here.

## Hardware status
**NOT RUN.** No live robot / R4.0 / R4.10 / movement / calibration under this directive.

## Working-tree status
Clean except this report, committed last so the tested-code SHAs (Gate A `e7acf6d`, Phase 1 `eaa7dbe`) are
distinct from the report SHA (parent `96a04ae`). `webui/dist/` gitignored; committed evidence under
`data/test-evidence/software/`.
