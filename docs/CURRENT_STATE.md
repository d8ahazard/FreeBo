# FreeBo — Current State

Snapshot of what exists right now. No history, no instructions — see ROADMAP.md for what's next and
PHASE0_ACCEPTANCE.md for the gates.

## Commit
- The Phase 0 software-gate work (`agent_next.md`) is COMMITTED in order: `c57b3f3` (evidence hygiene),
  `22ed083`+`e9ae29f` (sidecar/RtmNode atomicity + identity + epoch + correlated set_control + hard-forbidden
  raw), `eba3a5d` (motion ticket wired end-to-end), `9f04240` (reasoning cancellation), `10137a7` (readiness),
  `8030cbd` (full-suite teardown fix), `e8c1f66` (adversarial integration tests), `38764d2` (hardware harness
  rewrite), `6a11dda` (UI). Run `git log --oneline` for the exact set; `git status` should be clean.
- Frontend build provenance (asset name + content sha + source commit + stale flag) is live at `GET /api/state`
  under `build`, and printed at startup. `webui/dist/` is gitignored (built on deploy).

## Robots / transport (explicit)
- **EBO SE / EBO Air**: local LAN control via TUTK (Kalay P2P), MAVLink-over-RDT — `native_link.py`.
- **EBO Air 2**: CLOUD control plane via Agora RTM/RTC through the headless Node sidecar — `air2_native_link.py`
  + `scripts/rtm_sidecar.js`. **This path REQUIRES internet** (Agora). No verified local-only Air 2 control.
- **EBO Max**: **unverified** — not validated against hardware.

## Implemented (verified by unit/integration tests; NOT hardware-validated)
- **Central safety authority** (`autobot/brain/safety.py`): `SafetyFloor` owns a `ControlArbiter` (RLock) that
  is the single source of truth for latch / master-inhibit / monotonic epoch+generation, tokenized STOP
  dispatch, single-use compare-and-swap RESET admission, and motion-ticket admission/validation. Faculty
  decisions (`check_think/check_motion/check_drive/check_say/check_listen/check_see` + `capability_snapshot`).
  NOTE: a faculty/route audit confirms the physical DRIVE path is fully ticket-gated; non-drive effect
  commands (dock/laser/move_mode/avoid/release/resume) carry identity but are not yet per-command
  ticket-ENFORCED in the sidecar (tracked).
- **Master STOP / RESUME** (`/api/estop`, `/api/resume`): STOP gets a `StopToken` (epoch,generation,dispatch
  id), inhibits all faculties, latches motion, parks + CANCELS in-flight reasoning, cancels TTS, preempts the
  executor, and dispatches a link-level latched E-STOP stamped with the token's own generation+epoch (honest
  transport status). RESUME is admission-first + reconcile + single-use CAS; it stays inhibited (and re-latches
  the sidecar) if the reset is not reconciled.
- **Motion ticket end-to-end**: `ActionExecutor` admits a `MotionTicket` after the clamp, re-validates it
  immediately before `link.move`, and carries {generation,epoch} to the sidecar, which rejects a drive whose
  ticket was superseded by a STOP. Every physical drive route is ticketed (manual, overseer, calibration,
  locomotion, go_to_place).
- **Sidecar atomicity + identity** (`scripts/rtm_sidecar.js` + `rtm_node.py`): per-process + per-sidecar
  instance ids; correlated, validated `set_control` (rejects stale epoch/gen; a stale set_control can never
  unlatch after a newer STOP); `estop_reset` validates ALL preconditions before clearing the latch
  (fail-closed); in-flight STOP blocks RESET; immutable hard-forbidden raw set (env can never widen);
  `control_state().synchronized` requires bound instance + control_ready + matching epoch/gen/latch.
- **Reasoning cancellation**: a per-cycle generation token + boundary guards at every side effect; the whole
  cycle is gated on `check_think` (covers `/api/tick`, `/api/chat`, scheduled + command triggers); master STOP
  cancels the in-flight reason task. AI captioning is gated by `check_see`.
- **Truthful readiness** (`status_dict().readiness`): distinct fields (no generic `connected`) — rtm_connected,
  rtc_video_connected, sidecar process/control ready, process/sidecar instance ids, process/sidecar
  latched/epoch/generation, synchronized, stop_in_flight, reset_active, last_reconcile_error.
- **Honest UI**: jpost surfaces HTTP status; RESUME stays stopped + shows the exact error unless reconciled;
  STOP distinguishes inhibit vs degraded; capability heartbeat repairs the STOP banner; toggles never show an
  ability effective before the kernel reports it.

## Phase gate status
- **Phase 0 software safety gate: ACCEPTED FOR PHASE 1** (agent_next_3 Gate A). All physical effects — including
  `drive` — now require the full mandatory authority ticket, validated in the sidecar (`driveTicketError` /
  `effectOk`); the active-drive repeat + delayed-stop are bound to the full per-drive ticket.
- **Phase 0 physical gate: PENDING — HARDWARE NOT RUN.** R4.0 smoke + R4.10 acceptance not executed on the
  live Air 2. Hardware eligibility = NO. Physical movement is disabled by policy.
- **Phase 1 observability: AUTHORIZED.** Phase 2 / Phase 3: BLOCKED pending the R4.0 physical smoke.
- See `docs/ROADMAP.md` for the frozen Phase 0 software invariant list (may not be weakened without a reviewed
  migration). Do NOT describe Phase 0 as fully passed — software acceptance and physical acceptance are separate.
- **Air 2 requires the Agora cloud** (internet); no verified local-only Air 2 control. EBO Max unverified.

## Test gate
The canonical suite `python -X faulthandler -m pytest -q -p no:recording` passes and has run clean three
consecutive times on Windows (the per-test loop `socketpair` flake is fixed by a bounded retry in
`tests/conftest.py`). See PHASE0_ACCEPTANCE.md for reproducible commands + actual counts and `agent_results.md`
for the per-run evidence (committed under `data/test-evidence/software/<sha>/`).

## How the UI is served
`autobot/web/server.py` serves `webui/dist/index.html` + `/assets/*`. `webui/dist` is gitignored; build via
`cd webui && npm ci && npm run build` (bootstrap rebuilds when missing or stale).
