# FreeBo — Architecture Decisions

Durable decisions behind P0-R4. Keep terse; update when a decision actually changes.

## 1. SafetyKernel is the single faculty authority
`autobot/brain/safety.py` (`SafetyFloor`, acting as the kernel) is the ONE place that decides whether each
autonomous faculty may act. Callers (agent loops, audio sink, endpoints, skills, UI) must call
`check_think / check_motion / check_drive / check_say / check_listen / check_see` and act on the returned
`FacultyDecision`. They may pass context but must not independently re-interpret `allow_*`, `talk_enabled`,
`asleep`, the latch, or the master inhibit. Mechanical clamps (speed/duration/rate/scope) stay inside
`check_drive`. The kernel also produces the authoritative `capability_snapshot` consumed by the UI.

Decision type (`FacultyDecision`): `allowed, capability, reason, master_inhibited, requested_enabled,
effective_enabled, generation, ts`.

## 2. STOP / RESUME semantics
- **STOP** (`/api/estop`, voice STOP) = a **master autonomous-faculty inhibit**. Atomically (before any
  await): set master inhibit + motion latch + bump control generation. Then: cancel TTS, park reasoning,
  preempt the executor, link-level zero burst, autonomy→manual. Operator camera, telemetry, RTM health, and
  the UI control plane stay alive (they are recovery instruments, not autonomous faculties).
- **RESUME** (`/api/resume`) = the explicit operator lift. Reconcile the link/sidecar latch+generation FIRST;
  stay inhibited if the reset is not acknowledged; only then clear the process latch + master inhibit. Each
  faculty restores to its own requested toggle. Autonomy stays manual. Circuit-breaker HOLD is left intact
  (it has its own reset). Voice RESUME is intentionally absent — listening is disabled while stopped.
- **Master inhibit vs E-STOP latch vs HOLD**: the latch is the motion-specific part of a STOP; the master
  inhibit covers ALL faculties; HOLD (circuit breaker) is an orthogonal motion pause with its own reset.

## 3. Control generation + sidecar reconciliation
The SafetyFloor owns the authoritative generation (bumped once per STOP). Drives are stamped with the current
generation; the Node sidecar (`scripts/rtm_sidecar.js`) refuses drives that are latched or carry a stale
generation, and a freshly (re)started sidecar defaults motion-blocked until Python re-asserts the
authoritative latch+generation (`set_control` on connect). `RtmNode.control_state()` exposes process vs
sidecar latch+generation; a mismatch blocks motion. Raw RTM 101007 cannot reach the drive path, so it cannot
bypass the latch.

## 4. Operator video vs autonomous video
Three distinct intents over the one `MediaHub`:
- **Operator video** — `/api/video/preview.mjpeg` + `/api/snapshot.jpg`. Always available, including during
  STOP. A recovery instrument.
- **Safety/reflex video** — the looming reflex. A protection path, not an autonomous faculty.
- **AI semantic vision** — frames fed to the VLM/cortex. Gated by the `See` faculty (kernel `check_see`).
(Full physical separation of these subscriber intents is in progress — see CURRENT_STATE open blockers.)

## 5. Executor and future-arbiter boundaries
`ActionExecutor` is the ONLY path that issues AI motion + records motion evidence; it is preemptible and
honors HOLD + the safety floor. Manual/overseer motion goes through the same safety clamps but its own routes.
A future multi-source "arbiter" (Phase 1+) would sit above the executor to mediate competing intents; today
the safety floor + executor + behavior scope are the arbitration.
