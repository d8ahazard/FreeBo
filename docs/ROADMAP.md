# FreeBo — Roadmap

## Phase contract (authoritative)

Phase 0 has TWO separate gates:
- **Phase 0 software safety gate: ACCEPTED FOR PHASE 1** (agent_next_3 Gate A). The software safety architecture
  is accepted; Phase 1 may proceed.
- **Phase 0 physical gate: PENDING — HARDWARE NOT RUN.** The supervised R4.0 smoke + R4.10 acceptance have not
  been run. Physical movement stays disabled by policy until a supervised hardware gate is explicitly authorized.

Policy:
- Phase 1 may proceed after the Phase 0 software safety architecture is accepted (it is). Phase 1 exists partly
  to improve evidence + incident diagnosis BEFORE and DURING physical acceptance.
- Phase 2 (cognition/model benchmarking) and Phase 3 (personality) remain BLOCKED until the initial physical
  safety smoke gate (R4.0) succeeds.

Phases below are high-level; no speculative model names, token rates, or prices until independently verified at
the time the phase is authorized.

## Frozen Phase 0 software invariants

These are accepted and FROZEN. Future changes may EXTEND them but may not WEAKEN them without an explicit,
reviewed migration (and a test that proves the new invariant):
- All physical effects require a current authority ticket (epoch + generation + ticket id + matching process &
  sidecar identity), validated at admission, again before the stdin write, and a third time in the sidecar.
- Priority STOP is exactly-once per source and is never delayed behind normal action cancellation.
- RESET is prepared and committed (two-phase), fail-closed; only the RESET state machine clears a latch.
- Process and sidecar identities are exact (no None, no optional-field tolerance on safety/effect commands).
- A STOP invalidates in-flight reasoning and every stale effect; it never lowers accepted epoch/generation.
- Operator telemetry/video may survive a STOP; autonomous faculties (Think/Motion/Speak/Listen/See) do not.
- No production raw physical-effect bypass (only the audio call-mode ids are allowlisted; movement/dock/
  ownership/speed/avoid/actuator are immutably hard-forbidden on the raw channel).

## Phase 1 — Observability (AUTHORIZED)
- One canonical append-only structured event model for safety/faculty/reasoning/transport/motion events, with a
  bounded in-memory journal + durable JSONL (rotation/retention/redaction/shutdown flush/truncated-line recovery).
- Instrument every STOP source, RESET phase, faculty decision, effect admission/dispatch, reason/tool lifecycle,
  provider call, motion outcome, sidecar lifecycle, speech + AI-vision lifecycle.
- An authenticated query API (filters, pagination, correlation/incident traces, summary latency percentiles) and
  a timeline/inspector UI for after-the-fact incident review (requested vs effective, and why).
- A deterministic, redacted incident-export bundle.
- Physical movement remains disabled by policy throughout Phase 1.

## Phase 2 — Cognition / model benchmarking
- Only when authorized: enumerate the models actually available at that time and benchmark them on the real
  FreeBo workload — tool-call correctness, first-useful-audio latency, vision grounding, interruption
  handling, VRAM, queue stability, one-hour runtime, failure recovery.
- Keep the portability budget (single 12–24 GB GPU): one resident vision model; reasoning local-small or
  cloud; the nervous-system layers stay cheap/CPU. `LLM.md` is candidate input only.

## Phase 3 — Personality
- Persona, memory, and social behavior work — layered on top of a safe, observable, well-characterized motion
  + cognition base. Not before Phases 0–2 are solid.
