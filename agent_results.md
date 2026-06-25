# Agent Results

> WORK IN PROGRESS — `agent_next_4.md` ("Complete Observability and Prepare the Supervised Hardware Gate").
> Communicate only here; claims tied to tested SHA + command + exit code. Phase 0 software stays ACCEPTED/FROZEN;
> physical gate PENDING; hardware NOT RUN. This wave does NOT authorize hardware.

## Directive
`agent_next_4.md`: finish a practical Phase 1 observability system — journal v2 (restart-aware, non-blocking,
full redaction, health), one causal STOP→RESET incident model, complete runtime instrumentation
(reason/tool/transport/speech/vision + sidecar/system), a persistent + access-controlled query API, a live
timeline/incident inspector, and a software-only R4.0 rehearsal — leaving FreeBo ready for a separately
authorized supervised R4.0 smoke (not run here).

## Baseline SHA
- agent_next_3 Gate A tested: `e7acf6d6107b3c3c443e7da8196095022a76875d`; Phase 1 slice tested:
  `eaa7dbeb6e787bf80a3d17e1b0633f272a1e4db7`; report: `60f3edf2c75cd680ff525f544237f441efe99bc0`.
- Suite at baseline: 231 passed, 2 hardware-gated skips, exit 0. Real Node child-process tests ran. Hardware not run.
- Environment: Node v22.16.0, Python 3.10.11, win32.

## Review disposition
The agent_next_3 work is ACCEPTED and kept. These review findings are corrected as Phase 1 continuation (NOT
release blockers, NOT a phase downgrade):
1. The event API is same-origin only, NOT authenticated → adding a loopback-vs-owner-token access policy.
2. STOP/RESUME used different correlation ids (`stop-genN`/`reset-genN`) → adding ONE causal `incident_id`.
3. Query saw only the in-memory ring → journal v2 hydrates retained history + continues the cursor on startup.
4. Persistence flushed synchronously under the lock → journal v2 adds a background writer + bounded queue.
5. Redaction was strongest on `detail` → journal v2 normalizes EVERY envelope free-text field.
6. Time-range filtering was missing → added.
7. reason/tool/transport/speech/vision + detailed sidecar categories existed only on paper → wired in Gate 4.
8. Timeline fragment-key + 3s full-window polling → fixed in the live inspector.

## Tested code SHA
(pending)

## Phase status
(pending — set at completion)

## Journal v2
(pending — Gate 2)

## Causal incident model
(pending — Gate 3)

## Instrumented runtime paths
(pending — Gate 4)

## Persistent query and access policy
(pending — Gate 5)

## Live timeline and inspector
(pending — Gate 6)

## Software-only R4.0 rehearsal
(pending — Gate 7)

## Minor fixes rolled forward
(pending — Gate 8)

## Exact test commands and exit codes
(pending — Gate 9)

## Frontend build evidence
(pending)

## Machine-readable evidence paths
(pending)

## Known limitations
(pending)

## Hardware status
**NOT RUN.** No live robot / movement / R4.0 / R4.10 / calibration / hardware command under this directive.

## Working-tree status
In progress.
