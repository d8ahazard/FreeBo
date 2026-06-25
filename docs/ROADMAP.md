# FreeBo — Roadmap

Phase 0 (central safety + live faculty control + hardware acceptance) must be explicitly accepted before any
Phase 1 work begins. Phases below are intentionally high-level; no speculative model names, token rates, or
prices until independently verified at the time the phase is authorized.

## Phase 1 — Observability
- Structured, queryable logs/metrics for every faculty decision, STOP/RESUME, generation change, and motion
  outcome (build on the existing capability surface + `/api/metrics`).
- A timeline/inspector UI for after-the-fact incident review (what was requested vs effective, and why).
- Close out the remaining P0 hardening: root-cause the test-suite exit-hang, finish the deterministic
  STOP/toggle/sidecar/frontend test matrix, and the See-faculty frame-path separation.

## Phase 2 — Cognition / model benchmarking
- Only when authorized: enumerate the models actually available at that time and benchmark them on the real
  FreeBo workload — tool-call correctness, first-useful-audio latency, vision grounding, interruption
  handling, VRAM, queue stability, one-hour runtime, failure recovery.
- Keep the portability budget (single 12–24 GB GPU): one resident vision model; reasoning local-small or
  cloud; the nervous-system layers stay cheap/CPU. `LLM.md` is candidate input only.

## Phase 3 — Personality
- Persona, memory, and social behavior work — layered on top of a safe, observable, well-characterized motion
  + cognition base. Not before Phases 0–2 are solid.
