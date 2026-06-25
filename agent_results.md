# Agent Results

> WORK IN PROGRESS — `agent_next_3.md` ("Close the Final Drive-Ticket Gap, Then Begin Phase 1"). Communicate
> only here; every claim tied to a tested SHA + exact command + exit code. Phase 0 software is being accepted
> FOR PHASE 1 only; physical acceptance stays PENDING and hardware is NOT RUN.

## Directive
`agent_next_3.md`: Gate A — make the Node sidecar `drive` handler use the SAME mandatory effect-ticket validator
as every other physical effect (it currently uses bespoke partial validation: generation required, but epoch +
sidecar id optional, and process id + ticket_id not required), bind the active-drive repeat + delayed-stop to
the full per-drive ticket, harden queue invalidation, extend the static audit to the JS drive contract, add 12
deterministic Node child-process tests. Gate B — update the phase contract docs + freeze the Phase 0 software
invariant list. Gate C — deliver the first usable Phase 1 observability slice (structured event schema, bounded
journal + JSONL, instrumentation, query API, timeline UI, incident export, tests).

## Baseline SHA
- agent_next_2 tested code: `b445a0dca436afaddf8e6b58abaee82fe50ccefa` (210 passed, 2 skipped, 3 clean runs).
- agent_next_2 report: `09d2da9ad41158a5c25235e5bc56a620fa733b9b`.
- Environment: Node v22.16.0, Python 3.10.11, win32.

## The remaining defect (Gate A)
The sidecar `drive` branch (scripts/rtm_sidecar.js) validates `generation` (required) but treats `epoch` and
`sidecar_instance_id` as OPTIONAL and never requires `process_instance_id` or `ticket_id` — unlike the typed
effects which all route through `effectOk(c)`. The active-drive repeat re-checks only generation (a
same-generation/new-epoch transition would not kill it), and the queue pre-check uses only a generation compare.
Fix: route `drive` through the shared mandatory validator, bind the repeat + delayed-stop to the full ticket +
per-drive instance, and harden queue invalidation. (In progress.)

## Gate A tested code SHA
(pending)

## Phase gate status
(pending — set after Gate A is green)

## Final drive-ticket correction
(pending)

## Gate A exact tests and exit codes
(pending)

## Phase contract changes
(pending — Gate B)

## Phase 1 tested code SHA
(pending — Gate C)

## Observability architecture
(pending — Gate C)

## Instrumented paths
(pending — Gate C)

## Query API
(pending — Gate C)

## Timeline inspector
(pending — Gate C)

## Incident export
(pending — Gate C)

## Phase 1 exact tests and exit codes
(pending — Gate C)

## Known limitations
(pending)

## Hardware status
**NOT RUN.** No live robot / R4.0 / R4.10 / movement / calibration under this directive.

## Working-tree status
In progress.
