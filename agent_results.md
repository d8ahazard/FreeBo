# Agent Results

## Directive
`agent_next_4.md` — "Complete Observability and Prepare the Supervised Hardware Gate." Finish a practical Phase 1
observability system (journal v2; one causal STOP→RESET incident; full runtime instrumentation; persistent +
access-controlled query API; live timeline/incident inspector; software-only R4.0 rehearsal) and leave FreeBo
ready for a separately authorized supervised R4.0 smoke. This wave does **not** authorize hardware.

## Baseline SHA
- agent_next_3 Gate A tested `e7acf6d6107b3c3c443e7da8196095022a76875d`; Phase 1 first slice tested
  `eaa7dbeb6e787bf80a3d17e1b0633f272a1e4db7`; final report `60f3edf2c75cd680ff525f544237f441efe99bc0`.
- Accepted baseline as a successful Phase 1 transition. Suite at baseline: 231 passed, 2 hardware-gated skips,
  exit 0; real Node child-process tests ran; hardware not run.

## Review disposition
The agent_next_3 work is ACCEPTED and kept. The 8 review findings were corrected as Phase 1 continuation (no
phase downgrade):
1. Same-origin ≠ authenticated → added a loopback-vs-owner-token access policy on all observability/export
   endpoints (§Persistent query and access policy).
2. STOP/RESUME used different correlation ids → added ONE causal `incident_id` carried through the whole
   lifecycle (§Causal incident model).
3. Query saw only the in-memory ring → journal v2 hydrates retained history on startup + continues the cursor,
   and the query API scans the retained JSONL window (§Journal v2 / §Persistent query).
4. Synchronous flush under the lock → background writer + bounded queue + drop accounting (§Journal v2).
5. Redaction only strong on `detail` → every envelope free-text field is normalized/redacted (§Journal v2).
6. No time-range filtering → added (`start`/`end` UTC).
7. reason/tool/transport/speech/vision + detailed sidecar categories were paper-only → wired (§Instrumented
   runtime paths).
8. Timeline fragment key + 3s full-window polling → fixed in the live inspector (§Live timeline and inspector).

## Tested code SHA
- **Tested code SHA: `e7763058d6b7db23c8c2dc3032e7dbf44d623aa0`** (`e776305`). The full suite, the rehearsal, and
  the frontend build were all run at this SHA. The evidence commit (`17102ad`) adds only evidence files (no code),
  and this report is committed last, so its commit SHA is distinct from the tested code SHA.

Commit wave (cohesive):
- `333c7c5` baseline + review disposition
- `d9e1146` journal v2 + causal incident model
- `f3386c2` query API v2 + access policy + incident/health/readiness + live WS
- `8f59f35` one STOP→RESET incident + full runtime instrumentation
- `9a15776` software-only R4.0 rehearsal
- `4849761` focused lifecycle ordering tests
- `fb091c6` docs (Phase 1 complete for R4.0 + set_control limitation)
- `e776305` live timeline + incident inspector (frontend)
- `17102ad` machine-readable evidence

## Phase status
- **Phase 0 software gate: ACCEPTED, FROZEN**
- **Phase 0 physical gate: PENDING, HARDWARE NOT RUN**
- **Phase 1 observability: COMPLETE FOR R4.0**
- **Software rehearsal: PASS** (12/12 scenarios)
- **Ready for supervised R4.0: YES** — this is NOT authorization to run hardware. Stop and report.
- **Phase 2 cognition/model benchmarking: BLOCKED pending supervised R4.0**
- **Phase 3 personality: BLOCKED**

## Journal v2
`autobot/observability.py` evolved in place (no second logging system):
- Every event carries `process_session_id`; the durable sequence is monotonic and recovered on restart. The
  pagination cursor is **opaque** (`base64({process_session_id}|{seq})`) — it can never silently collide after a
  restart. Event ids are globally unique (uuid4).
- **Background writer**: `emit()` is synchronous only for build + FULL-envelope redaction + bounded ring insert,
  then a non-blocking `put_nowait` to a bounded queue. ONE tracked writer thread batches + periodically flushes;
  rotation/retention are owned by the writer; deterministic drain with a bounded shutdown deadline. Counters:
  enqueued, persisted, queue_dropped, persist_failed, undrained, recovered. A full queue NEVER blocks STOP/RESET/
  gating/dispatch — the event stays in the ring, a loss is counted, and the flood is not amplified.
- **Bounded streaming restart recovery**: discovers the active + rotated files, streams records (no `readlines`),
  tolerates malformed/truncated lines, restores the newest bounded history into the ring, continues the sequence,
  and emits a `journal_recovered` summary (files/valid/oldest/newest). Bounded by file count + max recovered
  events.
- **Full-envelope normalization/redaction** of `type/source/requested/effective/reason/outcome/correlation_id/
  command_id/detail` (+ ids): control chars stripped, lengths bounded; credentials/tokens/prompts/audio/image
  bytes/memory masked. No spoken text, transcripts, prompts, model responses, images, audio, or memory content is
  persisted — only counts/durations/labels/result classes.
- **Journal health** is exposed (`/api/events/health`, `/api/status`, summary): writer_alive, queue depth/
  capacity, persisted, queue_dropped, persist_failed, recovered, active_file_bytes, retained_files, oldest/newest
  timestamps. Journal failure is diagnostic only; it never weakens the safety system.

## Causal incident model
`ControlArbiter` opens ONE `incident_id` per master STOP (`current_incident_id()`, in the snapshot). The event
envelope adds `incident_id`, `parent_event_id`, `phase`. A master STOP and the subsequent RESUME emit phase-tagged
events under the SAME incident id: `inhibit` (master_stop) → `estop_dispatch` (transport: initial zero send +
retries) → RESET `admission` → `prepare` → `reconcile` → `finalize`/`resumed` (or degraded/superseded). A single
`/api/events/incident/{id}` query returns the whole STOP→RESET lifecycle in chronological order — carried through
the runtime transition context, NOT derived from generation arithmetic in the UI. Operation-level grouping still
uses `correlation_id` (`reason-genN`, `stop-genN`, `vision-…`, `speech-…`).

## Instrumented runtime paths
Wired and tested (only these are claimed):
- **safety.transition** (master STOP / motion latch / resume phases) and **control.effect_admission** (existing).
- **control.transport** — through `RtmNode` only (no second JS log file): `queued_to_sidecar`,
  `acknowledgement_received`, `timed_out`, sidecar `command_result`, and the agent's `estop_dispatch`.
- **system.lifecycle** (sidecar) — `sidecar_spawn`, `sidecar_ready`, `rtm_state`, `rtm_connected`,
  `session_refresh`, `sidecar_shutdown`.
- **reason.lifecycle** — `lock_wait_started` → `started` → `provider_wait_started` → `completed`/`cancelled`/
  `failed`; a stale/superseded cycle can NEVER emit `completed` (proven by test + rehearsal).
- **reason.tool** — `requested` → `completed`/`denied`/`failed` with argument KEYS only + result class + child
  action id (never argument/result payloads).
- **speech.lifecycle** — `requested`/`render_*`/`publish_*`/`playback_started`/`playback_completed`/`cancelled`/
  `stale_result_discarded` on both the WAV `publish_speech` and the mulaw `say_audio` paths; char/byte counts +
  duration + engine label + playback id only (no text/audio).
- **vision.lifecycle** — AI-facing caption/VLM only: `frame_selected`/`request_started`/`completed`/`failed`/
  `stale_result_discarded` with frame seq/age/bytes + model label + latency + result class (no pixels/text).
- **motion.lifecycle** (existing executor instrumentation).

## Persistent query and access policy
- `/api/events` filters: category/type/source/outcome/incident_id/correlation_id/process_session_id/command_id/
  event_id/epoch/generation/ticket_id, UTC `start`/`end`, `order` (asc|desc), opaque `cursor`, `persistent`,
  `limit`. The retained JSONL window is scanned (bounded) so **retained history is queryable** — proven by the
  restart-recovery test. Bad int/order params return HTTP 400.
- New endpoints: `/api/events/incident/{id}`, `/api/events/incidents`, `/api/events/health`,
  `/api/hardware_gate` (software-only readiness, ALWAYS `hardware_run=false`), `/api/status` (alias of
  `/api/state` used by the harness). Export takes `incident_id|correlation_id`, returns an attachment
  (`Content-Disposition`) with stable `schema_version=2`, and 503 if the journal is unavailable.
- **Access policy (honest):** loopback clients (the local UI session) are allowed; for a non-loopback bind, the
  observability/query/export endpoints require a configured `AUTOBOT_OWNER_TOKEN` via the `X-Owner-Token` header
  (never a query string), returning 401/403 on denial. This is proportional protection, not a general identity
  platform. The API is NOT described as authenticated for loopback — it is loopback-trusted.

## Live timeline and inspector
- Server broadcasts each journaled event as a thread-safe `{type:"journal_event", event, cursor}` WS envelope;
  `/ws` seeds a bounded recent catch-up window on connect. **No more 3s full-window polling.**
- `webui` Timeline tab rewritten into a live inspector: streams from a bounded (dedup-by-id) hook buffer; a
  one-shot AbortController catch-up fetch for filters/time-range (overlapping requests serialized/aborted);
  pause-live-while-inspecting; category/type/outcome/source + UTC time-range + incident filters; UTC/local
  toggle; incident list + detail trace; transport-stage latency view; journal-health indicator; copy event/
  correlation/incident id; export selected incident/range; the React Fragment key warning is fixed.

## Software-only R4.0 rehearsal
`python scripts/phase1_rehearsal.py` — uses ONLY the mock link + the real Node FAKE sidecar (no cloud, no
hardware, no movement). It drives the REAL instrumented paths and asserts each incident/correlation trace has the
required ordered events + terminal outcome for all 12 scenarios: startup/bind/sync; admitted motion via fake
transport; master STOP during active drive; failed initial E-STOP send; STOP→two-phase RESUME (one incident);
RESET invalidated by a newer STOP; reason cancelled during provider wait (never completes); speech render/publish/
cancel; vision stale-result discard; sidecar replacement/reconnect; journal queue pressure + persistence failure;
clean shutdown + restart recovery. Result: **12/12 PASS**, and the report states `hardware_run=false`,
`physical_acceptance=false`, `ready_for_supervised_R4_0=true`. This is a software rehearsal FOR R4.0 — NOT R4.0
and NOT a physical acceptance run.

## Minor fixes rolled forward
1. `configure()` drains/closes the prior journal before replacement (also in tests). ✓
2. `readlines()` recovery replaced with bounded streaming recovery. ✓
3. Invalid integer/order query params → HTTP 400. ✓
4. Export → 503 when the journal can't produce the result. ✓
5. Export uses an attachment filename + stable `schema_version`. ✓
6. Software provenance cached once at import (no `git` subprocess per export). ✓
7. Startup background pollers (telemetry/audio/daily-memory) are tracked + cancelled/awaited on shutdown. ✓
8. React Fragment key added; overlapping timeline requests aborted/serialized. ✓
9. The connect-time assert-latch-only `set_control` limitation is documented (only the two-phase release clears a
   latch); not redesigned (no test proved it unsafe). ✓

## Exact test commands and exit codes
Environment: win32 (Windows 10.0.26200), Python 3.10.11, Node v22.16.0.
- Focused groups (all exit 0): `tests/test_observability.py` (14), `tests/test_observability_api.py` (8),
  `tests/test_control_arbiter.py`, `tests/test_reason_cancellation.py`, `tests/test_priority_stop.py`,
  `tests/test_estop_endpoint.py`, `tests/test_api_contract.py` (these 7 groups = 51 passed),
  `tests/test_rtm_node.py` + `tests/test_sidecar_protocol.py` (33, **real Node child-process tests ran**),
  `tests/test_speech.py` + `tests/test_adversarial_integration.py` (23), `tests/test_lifecycle_traces.py` (3),
  `tests/test_phase1_rehearsal.py` (2).
- Rehearsal: `python scripts/phase1_rehearsal.py` → 12/12 PASS, exit 0.
- **Canonical full suite** (at `e776305`): `python -X faulthandler -m pytest -q -p no:recording` →
  **243 passed, 2 skipped, exit 0** in ~83s.
- Skipped tests (both intentional, `--hardware`-gated):
  - `tests/test_hardware.py::test_* [1]` — needs --hardware + a running app
  - `tests/test_hardware.py::test_* [2]` — needs --hardware + a running app

## Frontend build evidence
`cd webui ; npm ci ; npm run build` → tsc clean, exit 0.
- entry JS `assets/index-BR_lsTOm.js` sha256 `FA856A1B887E068076E8ECF4A1D4FEE7671F18D164215D5759AA9497FADEE91B`
- CSS `assets/index-BfsfA-T8.css` sha256 `85C8A1DB3716BA29103596CF06CEE1ABBC9C350D3D1EF95DBBEF5EB50CE03AE7`
- `index.html` sha256 `BBF1B84DA0B6EEE1D3E52F79F477C6051697AD4AAD5590DCE95C992E3B5F65E4`

## Machine-readable evidence paths
Under `data/test-evidence/software/e7763058d6b7db23c8c2dc3032e7dbf44d623aa0/`:
- `summary.json` — counts, exit codes, env versions, frontend hashes, phase status.
- `fullsuite.txt` — raw full-suite output (243 passed / 2 skipped / exit 0).
- `rehearsal/rehearsal_report.json` — 12/12, ready_for_supervised_R4_0=true, hardware_run=false.
- `rehearsal/scenario_*.json` — redacted per-scenario incident bundles.

## Known limitations
- The observability API is loopback-trusted; remote access requires an owner token. It is not a full auth
  platform and is not claimed to be authenticated for loopback.
- Live WS delivery uses `loop.call_soon_threadsafe` from worker threads; under extreme event rates the WS stream
  is best-effort (the durable journal + cursor catch-up remain authoritative). The client buffer is bounded.
- The rehearsal exercises mock/FAKE components only; it proves the observability TRACES, not physical behavior.
- The connect-time `set_control` remains assert-latch-only (documented); unchanged this wave.
- `command_id` correlation across the Python↔sidecar boundary is per-acked-command; drive repeats emit only the
  sidecar `sent` signal (by design — repeats are not separately acked).

## Hardware status
**NOT RUN.** No live robot, movement calibration, R4.0, R4.10, or any hardware command was executed under this
directive. Hardware eligibility remains gated on a separately authorized, supervised run.

## Working-tree status
All code committed in the wave above; tested code SHA `e776305`; evidence `17102ad`; this report is committed
last (distinct SHA). `webui/dist/` is a gitignored build artifact (rebuildable; hashes recorded above). No live
robot was contacted.
