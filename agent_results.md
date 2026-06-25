# Agent Results

> Report for `agent_next_2.md` ("Close the End-to-End Safety Authority"). Every claim is tied to a tested code
> SHA + exact command + exit code + platform/versions. Phase 0 = FAIL, hardware = NO (unchanged throughout).

## Directive
`agent_next_2.md` — close the cross-process safety-authority gaps from the prior wave: one strict control
protocol with mandatory identity + effect tickets; a prepared two-phase RESET (no split-state interval); a
synchronized RtmNode state machine; removal of every unticketed robot-effect path; priority-first, exactly-once
STOP; complete reasoning/faculty cancellation; a coherent dark/wake lifecycle; a stronger (un-run) hardware
harness; forced-race adversarial tests; a static authority audit; UI/API contract tests; root-cause the
full-suite hang; and an honest report.

## Baseline commit
- Previous directive commit: `0fd1263091676cf910ab3e2a9f36632f2235fdd2`
- Previous tested-code commit (prior wave): `383bd8674eeda209f8e77627e0688f349246b4db`
- Previous report commit: `af9fedacaaad5422563a46dd910a0fa54ae7afb9`
- This wave started from: `4c1a0cdbf02efdd1ea7bda27bd5e2ac1b274d927`.
- Environment: Node v22.16.0, Python 3.10.11, win32.

## Tested code commit SHA
`b445a0dca436afaddf8e6b58abaee82fe50ccefa` — the clean-tree SHA the focused groups + the 3x canonical full
suite were run against. (The only commits after it before this report add committed evidence + this report; no
tested code changed.)

## Report parent SHA
`eb47207ac6ff8cfb26e763863061d9eab24f6294` (the commit this report is committed on top of). A report cannot
embed its own commit SHA; the containing commit is visible in git history.

## Phase 0 verdict
**FAIL.** Software does not authorize hardware; supervised physical acceptance is separate and was not run.

## Hardware eligibility
**NO.** No live robot / R4.0 / R4.10 / movement / hardware command was run. The hardware harness was
strengthened + unit-tested but never executed against the robot.

## Review corrections
The prior wave's report over-claimed; corrected and now actually fixed:
1. `set_control` correlation — now correlated-validated; startup binds `ready` before connect; `set_control` can
   only assert a latch (never clear). (§3; documented deviation: the connect-time reconcile still uses a
   fire-and-forget assert-latch `set_control`, which the sidecar can no longer misuse to unlatch — a blocking
   correlated ack on the manager thread would deadlock its own reader. The full correlated-ack startup is
   deferred and noted in Known limitations.)
2. Non-drive effects ticketed — `dock/avoid/laser/release/resume/dock_release/move_mode/move_speed` + eyes now
   go through admit_effect + ticketed correlated dispatch; no `rtm._send` outside rtm_node (§4, §10 enforces).
3. Mandatory identity/epoch/generation/ticket on effect + transition commands (§1, §4.5 sidecar `effectOk`).
4. Atomic RESET — replaced single-phase unlatch with prepared two-phase release (§2).
5. Reasoning cancellation — all reason invocations tracked; the provider-blocked test is mandatory (no skip);
   reason guard uses live settings (§6).
6. Harness consumes nested transport evidence + real acceptance calcs (§8).
7. One canonical tested-SHA record + actual counts (this report; no memorized number).

## Implemented transitions
§1 (`dd2d6d2`): closed `EFFECT_*` class set + `EffectTicket{epoch,generation,effect_class,ticket_id}`;
`ControlArbiter.admit_effect` issues unique non-zero tickets gated on not-inhibited/latched/STOP-in-flight;
`SafetyFloor.admit_effect(class, source, settings)` is the single policy authority. Command results split
`protocol_valid`/`queued_to_sidecar`/`control_state_applied`/`sdk_send_*` and never claim `sent_to_agora` for a
local mutation.

## Two-phase RESET evidence
§2 (`7c86e93`): `ResetToken` reserves a strictly-newer (release_epoch, release_generation);
`arbiter.finalize_reset` atomically installs it only if no STOP raced. Sidecar `prepare_reset` (validate
identity + exact state + release-newer + control-ready; store one record + nonce; STAY latched; no SDK send) +
`commit_reset` (re-validate same prepared reset, no STOP after prepare, atomic install + clear latch + consume
nonce). `RtmNode.reset_reconcile` = prepare→commit, fail-closed. `/api/resume` = sync-preflight + admission +
reconcile + finalize; a post-commit STOP race stays inhibited + re-latches (degraded-critical). Parent-pipe
end / SIGTERM fail-safe latches + invalidates the prepared reset + zero-sends.
Barrier-forced cases: stop-after-prepare-invalidates-commit, reused-nonce-rejected, two-simultaneous-prepares→
one, prepare-rejected-while-estop-initial-zero-BLOCKED (FAKE block seam, no sleep), parent-death→new-instance-
starts-latched, set_control-cannot-unlatch.

## Priority STOP evidence
§5 (`a0441e7`): `emergency_stop` begins the true `link.estop(token)` IMMEDIATELY, then cancels the executor via
`cancel_active(dispatch_stop=False)` (no ordinary stop racing ahead of the hard stop), then awaits/reports.
Never self-cancels the STOP caller. Voice STOP dispatches the master STOP exactly once (no duplicate `command`).
Sidecar STOP always latches+zeros but NEVER lowers accepted epoch/generation (reports token current|newer|
stale). Tests: master STOP issues exactly one priority estop (no preceding ordinary stop); voice/barge-in/tool
STOP each dispatch once; stale STOP latches without regressing state (`test_priority_stop.py`,
`test_sidecar_protocol.py`).

## Effect-ticket audit
§4 (`a54da84`) + §10 (`1349a3a`): air2 `move/drive` REQUIRE a ticket (no `_ticketed` fallback); all effects go
through admit_effect + correlated ticketed dispatch; the sidecar `effectOk` requires mandatory
identity+epoch+gen+ticket (MISSING is rejected, not just stale) and refuses effects while latched/mid-STOP. The
static audit (`tests/test_authority_audit.py`, 4 passed) fails on `rtm._send(` outside rtm_node, physical-effect
`raw(` (only audio 102001/102003 allowlisted), `sent_to_agora=True` on local mutations, and a re-introduced
un-ticketed motion fallback.

## Reason/faculty cancellation evidence
§6 (`4dc4808`): all reason invocations tracked in a set (a waiter can't hide the provider-blocked owner); master
STOP/Think-off cancel every stale one except the STOP executor; `_reason_guard` uses a FRESH settings snapshot;
the perceiver gates AI image intake on `check_see` (telemetry/operator preview unaffected); mic feed_speech
requires live Listen (STOP keyword excepted); overseer speech via SpeechService. Tests
(`test_reason_cancellation.py`): provider-blocked cycle cancelled to no-side-effect (MANDATORY, no skip);
Think-off during provider await cancels before the returned tool runs; concurrent running+waiting reason both
cancelled; `/api/chat` + `/api/tick` while inhibited → HTTP 423, no transcript mutation (`test_api_contract.py`).

## Sidecar lifecycle evidence
§3 (`e4127a4`) + §7 (`257912b`): RtmNode has a `_state_lock` (RLock); reads `ready` + binds the exact sidecar
instance before connect; strict pending-command KIND correlation (wrong-kind/replaced-instance result can't
satisfy a waiter or mutate state); `synchronized` requires bound instance + control-ready + exact
epoch/gen/latch + matching process id (no None); deterministic shutdown joins the tracked stderr thread. Cadence
NEVER sends avoidance-off while latched (ownership keepalive may continue, documented). `connection()` is a
coherent pause_control/resume_control lifecycle with no `rtm._send`. Tests: wrong-kind correlation; release
refused while latched but ownership-resume allowed.

## Harness status
§8 (`4925b99`): `scripts/hardware_smoke.py` consumes nested `transport_result` evidence without inference;
`percentile()` + `acceptance_report()` compute the §8.3 gates (STOP p95<600, ack p95<1200, motion dispatch
p95<250, TTS cancel p95<300, every-STOP-observed-halt, no-post-STOP-motion, stale-effect-rejected); a missing
measurement is a FAIL and the report never passes when ineligible (`--auto`/dirty tree). Unit-tested
(`tests/test_hardware_harness.py`, 11 passed). NOT run against the robot.

## Exact test commands and exit codes
All `python -X faulthandler -m pytest -q -p no:recording ...`; Node v22.16.0, Python 3.10.11, win32.
- Focused group A `tests/test_control_arbiter.py tests/test_rtm_node.py tests/test_sidecar_protocol.py
  tests/test_adversarial_integration.py tests/test_estop_endpoint.py tests/test_authority_audit.py` →
  **63 passed**, exit 0.
- Focused group B `tests/test_action_executor.py tests/test_reason_cancellation.py tests/test_safety.py
  tests/test_speech.py tests/test_bargein.py tests/test_priority_stop.py tests/test_api_contract.py` →
  **57 passed**, exit 0.
- Focused group C `tests/test_hardware_harness.py` → **11 passed**, exit 0.
- Canonical full suite (no path) ×3 fresh runs at `b445a0d`: **210 passed, 2 skipped**, exit 0 each
  (~72–85s). No pending-task warning, no socketpair hang, no per-test timeout.

## Machine-readable evidence paths
`data/test-evidence/software/b445a0dca436/` (committed): `fullsuite_run{1,2,3}.txt` (raw outputs), `summary.json`
(tested SHA, clean tree, command, env versions, exit codes, per-run raw SHA-256, frontend bundle hashes),
`_env.txt`.

## Frontend build evidence
`cd webui && npm ci && npm run build` (tsc clean; only the upstream agora-rtm-sdk eval warning). Source SHA
`ca6c44a` (no webui source changed in this wave — the §9 UI honesty work landed in the prior wave, so the bundle
is byte-identical). `webui/dist/` is gitignored (built on deploy). Served bundle:
- entry JS `assets/index-BH4iPHqj.js` SHA-256 `AE369BD48C8477647BAC95D6D2341FDB99DB396D9C666CF476DFBCBE6C3A75EC`
- entry CSS `assets/index-gSsarUf9.css` SHA-256 `78ACDB5059F2208F7291DEA7F659759484D0BA0F4FC1D0C89459508B037F7FB5`
- `index.html` SHA-256 `C03E887A9132F251C22F882D6C8753AAE8C442BF6F560F799D37C5C1DBE5F137`

## Known limitations
- HARDWARE NOT RUN. Phase 0 = FAIL, hardware eligibility = NO.
- Skipped tests: ONLY `tests/test_hardware.py` (2 cases) — gated on `--hardware` + a running app. No other test
  skips; the provider-blocked cancellation test now always runs.
- §3 connect-time reconcile uses a fire-and-forget assert-latch `set_control` (the sidecar can no longer use
  `set_control` to unlatch, so this cannot weaken safety); the full correlated-ack startup with a separate
  reader/manager thread split is DEFERRED to avoid a manager-thread/reader deadlock.
- The hardware harness's full scenario setup/cleanup scripting (held drive, active TTS/STT/caption, RTM
  reconnect, sidecar replacement) is partial; the acceptance threshold math + abort rules are implemented +
  unit-tested, but the live scenario drivers are not (the harness is not run).
- Air 2 control REQUIRES the Agora cloud (internet); no verified local-only Air 2 path. EBO Max unverified.

## Working-tree status
Clean except this report, committed last so the tested-code SHA (`b445a0d`) is distinct from the report SHA.
`webui/dist/` stays gitignored; committed software evidence lives under `data/test-evidence/software/`.
