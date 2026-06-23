# FreeBo — Native EBO Air 2 link (`air2_native`)

The Air 2 is a **cloud** robot: control = Agora **RTM**, media = Agora **RTC**. `air2_native` runs BOTH
server-side with **no browser**:

```
Air2NativeLink (autobot/robot/air2_native_link.py)
 ├─ control  → RtmNode (autobot/robot/rtm_node.py)  → scripts/rtm_sidecar.js  (real Agora RTM SDK, headless Node)
 └─ media    → AgoraNativeReceiver (autobot/robot/agora_native.py, aiortc) → MediaHub → UI / brain(VLM) / VSLAM / STT
```

One cloud session (EboCloud.create_session) feeds both halves; a shared provider re-mints it (cached ~180s).

## Smart behavior (the decision policy)

`vlm_service.decide` is mode-aware and goal-directed, not random forward/turn:
- **Explore:** FORWARD is primary (make progress through open space); only when blocked does it turn,
  preferring a detected doorway/opening, with a periodic look-around. Forward-primary is what prevents the
  spin-in-place failure mode.
- **Command (directive):** detects the target ("do you see <directive>?"), turns toward it / advances to
  pursue; scans when it can't see it.
- **Conversational:** holds position, only rotates to keep the person centered.
- **People:** approaches & greets — turns toward the person, advances when centered, happy eyes.
- **Speech is in-character:** moondream is prompted as `{robot_name}, {persona}` so spoken lines fit the
  personality (junk like the on-screen "EBO" text is filtered). Speech only on describe/heard cycles so the
  drive loop stays fast.
- **Memory:** notable sightings (people) + periodic observations are logged to `Memory` so FreeBo builds a
  sense of its space over time.

## Brain (modular, light — runs on a potato)

MiniCPM-o omni was removed: 8B, and loading even 4-bit materialized the **16 GB fp16 in RAM** → with 32 GB
RAM the box thrashed and crashed. The brain is now modular:

- **See:** `scripts/vlm_service.py` — MiniCPM-V 4.6 (a Qwen3.5 hybrid, ~2.6 GB VRAM) at `AUTOBOT_VLM_URL`.
  `/vlm/decide` has two paths: a **fast nav** path (ACTION+EYES only, ~1.2s warm on an RTX 3090) used for
  ordinary roaming cycles, and a **full reasoned** path (SEE/THINK/ACTION/EYES, ~4.5s) used when
  `describe=true`, `mode=command`, or speech was heard — i.e. the cycles where the brain actually speaks/logs
  the reasoning. Run the service on its dedicated venv via `scripts/run_vlm.ps1` (see "VLM service env").
- **Hear:** faster-whisper (`AudioSink` on the MediaHub → `brain.feed_speech`).
- **Speak:** Piper (`autobot/brain/tts.py`). UI plays the WAV (`speech` WS event). Robot-speaker output goes
  through the native RTC audio publisher (G.711 A-law; see "Talkback" below).
- **Decide:** `AgentBrain._reason_vlm` — frame → moondream → drive/eyes; mode + toggles + safety enforced.

`.env`: `AUTOBOT_AI_PROVIDER=vlm`, `AUTOBOT_VLM_URL=http://127.0.0.1:8360`, `AUTOBOT_ROBOT_LINK=air2_native`.

## Lifecycle — the "drops motion control regularly" bug (root cause + fix)

Symptom: motion control dies after a while; a manual command / moving the robot seemed to revive it.
Root cause: **orphaned `rtm_sidecar` node processes**. Force-killing `python -m autobot` (or any unclean stop)
orphans the Node sidecar, which STAYS logged into RTM. Each restart added another — N sidecars all logged in
with the **same RTM uid** → uid conflict → every `sendMessageToPeer` fails `Error 102 (NOT_LOGGED_IN)` while
receive still works (so telemetry flowed but drives died).

Fixes (DO NOT REGRESS):
- `RtmNode._kill_stale_sidecars()` runs BEFORE spawning — kills any existing `rtm_sidecar` node procs (psutil,
  OS fallback). Guarantees exactly ONE logged-in controller.
- Sidecar single-flight `connect()` guard + clean `teardown()` (logout + removeAllListeners + clearDrive).
- Re-login ONLY on a TERMINAL `ABORTED` state or SUSTAINED send failures (≥6), heavily throttled (12s).
  Do NOT re-provision on the normal `CONNECTING/LOGIN`/`RECONNECTING` states — that spirals into overlapping
  logins (the exact thing that caused the conflict). 102 = our login; recover with a FRESH session.
- After `login()`, wait for the CONNECTED state before the first sends (avoids a startup 102 burst).

Verified: single sidecar, `connected=True` held steady, 0 new send errors over a multi-minute window.

## Control protocol (RTM) — KEEPALIVE IS REQUIRED, not reconnect

The robot drops the control session without a steady heartbeat. The sidecar (`rtm_sidecar.js`) sends:

- `101005` keepalive **every 2 s** (the control-plane heartbeat — do NOT remove),
- `101003` controller-login + `103045` avoid **every 30 s**,
- `101007` drive: **sustained** (resent every 200 ms for the move duration), because the robot has a drive
  deadman — a single frame barely twitches. Forward = negative `ly`; scale to ±100; `rx` = turn.

Drive speed IS graded: `ly`/`rx` magnitude (0..1) × 100, clamped to `config.max_speed` by the safety floor.

**Control suppresses the robot's autonomy.** While we hold the keepalive (101005) heartbeat, the robot stays
in remote-controlled mode and will NOT run its onboard low-battery return-to-dock — it'll drain out on the
floor. To hand control back: `release` (stop the heartbeat, stay logged in for telemetry) / `resume` (re-claim)
/ `dock_release` aka the `go_home` action (dock + release). Auto-dock (`_maybe_autodock`) now uses `go_home`
so the robot docks itself while it still has charge. NOTE: a dead robot (0%) can't move regardless — release
won't help at 0%; it must be charged manually. Edge case: a token-expiry reconnect re-claims control via
`startControlCadence()` — fine in normal use, but if it preempts an in-progress go-home, dock again.

## Media plane (RTC) — stall watchdog (fixes "had to sleep/wake")

ICE consent (aioice STUN) can fail mid-session (`TransactionTimeout`); the media silently dies and the brain
goes blind → motion stops. The receiver runs a **frame-stall watchdog**: if no decoded frame for ~6 s it
tears down and `_run_forever` rejoins automatically — no manual sleep/wake.

White / distorted frames are **packet-loss corruption**: an H265 access unit decoded with a missing RTP
packet produces garbage. The receiver now:
- tracks RTP sequence numbers per SSRC and **drops an access unit if a packet gap occurred inside it**,
- **skips frames until the first keyframe** (decoding P-frames with no I-reference = garbage).

## Telemetry — robot pushes COMPRESSED RAW RTM messages (this is the gotcha)

The robot streams status ~1-2/s over **RTM peer messages**, but as **RAW (binary) messages, zlib/deflate
compressed** — so `message.text` is EMPTY. The sidecar must read `message.rawMessage`, inflate it, then JSON
parse (`rtm_sidecar.js::_msgText`). Decoded payloads (verified live):

Full inbound telemetry id catalog (decoded live; the robot pushes only these three, ~1-2/s):
- `id 101004` — device info (model, sn, mac, wifiSsid, ip, fw versions).
- `id 101026` — **status**: `data.battery = {level, percentage, chargeStatus}` (battery is NESTED — parse
  `data.battery.percentage`, not `data.percentage`), `sdcard`, `status{liveStatus, laserStatus, isVideoRecording}`.
- `id 101028` — **settings**: `moveSpeed`, `moveMode`, `lowBatteryPercentage`, `avoidobstacle`, volumes, etc.
  Only appears AFTER we send `101027` (subscribe/ack) on connect — without that the robot withholds it.

### 6-axis IMU — NOT in the Air 2 cloud stream (verified)
Exhaustively mapped (incl. the `101027` subscribe that unlocks the extended stream): the Air 2 pushes only
the 3 ids above. There is **no raw accel/gyro (6-axis IMU)** in its cloud telemetry — it's used internally
(self-righting/avoidance) but not exposed. The 6-axis IMU exists on the **EBO SE over LAN MAVLink** only
(`eboproto`: `RAW_IMU` msgid 27 ax/ay/az, `ATTITUDE` msgid 30 roll/pitch/yaw). `parsePeer` already extracts
`imu`/`gyro`/`tof`/`touch` if a future firmware id emits them.

`/api/debug/rtm` dumps rtm connectivity + recent raw decoded payloads + parsed telemetry — use it to map more
fields (IMU/TOF live on other ids; extend `parsePeer` as they're identified).

## LAN probe — the Air 2 has NO local surface (verified, can't "be the cloud")

Full `nmap` of the robot (`192.168.1.33`, mac `a8:b5:8e:93:b3:29`) on the LAN:
- **All 65535 TCP ports CLOSED** (RST, not filtered) — the robot listens on **zero** TCP. No RTSP, no HTTP,
  no ONVIF, no UPnP, no local API.
- **UDP** top-150: only no-response (`open|filtered`) + ephemeral high ports (its outbound RTC/P2P source
  ports). No inbound UDP *service* to talk to.

So the Air 2 is a **purely outbound cloud client** with no inbound LAN interface. "Being the cloud server"
locally is **not feasible** on this model:
- Control + media ride **Agora's proprietary, encrypted, authenticated RTC/RTM network** (the robot resolves
  `*.agora.io` + Agora's AP lookup and speaks Agora's closed gateway protocol). We cannot reimplement or
  impersonate Agora locally; redirecting its DNS just breaks the connection.
- Enabot's bootstrap is **signed HTTPS REST**; even MITM'd it only yields Agora tokens — which we already
  capture via the collector, so we mint sessions ourselves.
- TUTK/Kalay LAN control (the SE's local path) is **closed on the Air 2**: `avClientStartEx` → `-20015`.

What we already do is therefore the only viable path: **join the same Agora channel as a peer** (captured
creds + a minted session) so we ARE the controller via the cloud relay. True offline/local control is a
property of the **EBO SE** (LAN MAVLink over TUTK), not the Air 2. The only realistic "less cloud" gain on
Air 2 is Agora **LAN P2P for the media leg** (signaling still cloud) — a latency/bandwidth win, not offline.

Battery/charge now flow end-to-end (verified: `battery:73, charge:0`). NOT in the cloud REST (`robots/robot`
has only machine/agora/tutk info). The gateway signaling WS carries only `on_p2p_ok` + stream announcements,
NOT telemetry.

## Talkback (robot speaker) — native RTC publish ✅

The robot plays audio published into the Agora **RTC** call by any broadcaster. Native (`air2_native`)
publishes server-side and **works on hardware** (confirmed audible from the robot speaker). Reverse-engineered
from the EBO app's own bytecode (`com.enabot.lib_device.agora.f.j`), not guessed:

- **Codec:** G.711 **A-law (PCMA), payload type 8**. The app pins audio to the G.711 family
  (`che.audio.codec_unfallback:[0,8,9]`, `che.audio.custom_payload_type:8`); the Air 2 announces its own mic
  as pt 8. We publish PCMA pt 8 to match.
- **Role:** the join is a **broadcaster** (`role:"host"`, LIVE_BROADCASTING-equivalent). The robot plays any
  broadcaster's published audio once intercom is on — there is no special publish trick, just publish + enable.
- **Wire path:**
  1. `build_rtp_capabilities()` adds a PCMA **send** codec to the join `ortc.rtpCapabilities` (without a
     declared send codec the gateway drops our RTP). Enabled when talkback is on.
  2. On the first clip, `Air2NativeLink._open_call_mode()` replays the app's exact handshake:
     RTM **`102001 {open:1}`** (open audio session) → wait → **`102003 {open:1}`** (intercom app→robot ON).
  3. `AgoraNativeReceiver._send_publish_offer()` sends the gateway `publish` offer
     (`{state:"offer", ortc:[{stream_type:"audio", ssrcs:[{ssrcId}]}]}`) so our SSRC is registered, then the
     publish loop streams 20 ms G.711 A-law RTP (silence keepalive + queued TTS).
- The brain/web render the WAV and call `Air2NativeLink.publish_speech(wav)` (TTS stays out of the robot
  layer). Gated by **`AUTOBOT_AIR2_NATIVE_TALK=1`** (default off; it switches the join to publish-capable caps).
- Standalone check: `python scripts/play_on_robot.py [clip.wav]` (defaults to a test tone).

**Session-loop robustness (the bug that hid this):** a single bad/unexpected signaling frame must never drop
the RTC session. The hold-open loop now splits *recv* (connection errors → rejoin) from *per-message
processing* (errors logged + skipped). Teardown also sends an explicit gateway **`leave`** and swaps the RTP
tap for an **async** no-op (a sync no-op crashes aiortc's `await`-ed handler) so retired sessions don't zombie
the robot's viewer slot or feed frames into the next session.

## Diagnostics endpoints (added for the self-test)

- `GET /api/telemetry` — live telemetry mirror (now includes `video_frames` + `audio_frames`).
- `GET /api/diag/heard` — recent STT transcripts (proves the hear path end to end).
- `GET /api/selftest` — runs the live capability self-test in-process and returns a JSON report.

## Status

| Capability | State |
|---|---|
| Native RTM drive/eyes/dock | ✅ |
| Native RTC video decode (H265) | ✅ |
| Smooth roaming (fast nav + sustained drive) | ✅ |
| Media stall auto-recovery | ✅ (watchdog) |
| Corrupt-frame suppression | ✅ |
| Hear (whisper) | ✅ wired (needs robot audio stream) |
| Speak in UI (Piper) | ✅ |
| Speak on robot speaker (RTC publish) | ✅ confirmed audible (G.711 A-law pt8 broadcaster publish + 102001/102003 + publish offer — `AUTOBOT_AIR2_NATIVE_TALK=1`) |
| Closed-loop motion confirmation (frame-diff + VSLAM) | ✅ (`confirm_motion`) |
| Live capability self-test | ✅ (`scripts/robot_selftest.py`, `/api/selftest`, UI panel) |
| Battery / charge telemetry | ✅ (RAW RTM, `data.battery.percentage`) |
| Laser(IR) / moveSpeed / moveMode / low-batt / avoid | ✅ (ids 101026/101028, wired to HUD) |
| Raw 6-axis IMU (accel/gyro) | ❌ not in Air 2 cloud stream (SE LAN-MAVLink only) |
| Anti-stuck navigation | ✅ (turn every 5th forward) |
| Stable `connected` (no OFFLINE flicker) | ✅ (RTM up OR fresh video) |
| UI shows correct brain | ✅ (`moondream2 · whisper · piper`) |

## Restart = alive (verified)
Clean `python -m autobot` restart with `AUTOBOT_ROBOT_LINK=air2_native` + `AUTOBOT_AI_PROVIDER=vlm` +
`AUTOBOT_AUTONOMY=auto`: the robot comes up roaming on its own (12 drives/20s), connection stable, telemetry
flowing, 0 errors. The vision service (`scripts/vlm_service.py`, started via `scripts/run_vlm.ps1` on the
`D:\vlm-venv` venv, port 8360) must be running — it persists across app restarts; the RTM sidecar is spawned
by the app each start.

## VLM service env (MiniCPM-V 4.6 + flash-linear-attention on Windows)

The model's `linear_attention` layers only run fast with flash-linear-attention (fla) Triton kernels, which
need a torch/triton pair where `torch.compile` works. The global Python had a broken torch 2.5.1 + triton
3.2.0 combo (fla would deadlock), so the service runs on a dedicated venv:

- `D:\vlm-venv`: torch 2.7.1+cu126, triton-windows 3.3, transformers 5.12.1, fla (`fla-core` +
  `flash-linear-attention`), torchvision, accelerate. `causal_conv1d` is intentionally absent (torch
  fallback for that cheap conv; only fla matters for speed).
- Start with `scripts/run_vlm.ps1` (sets `HF_HOME`, `TRITON_CACHE_DIR`, frees port 8360). `vlm_service.py`
  also sets `FLA_USE_CUDA_GRAPH=1` and `TRITON_CACHE_DIR`, and **warms the Triton kernels at startup** so the
  first real request is fast. First-ever kernel compile is ~90s; cached restarts are ~15s.
- Latency is decode-bound (~110 ms/tok): the eager HF generate loop fires ~9k micro-kernels/token on a
  ~1.3B model, so it's launch-bound, not linear-attention-bound (fla is already engaged; `torch.compile`
  can't graph it because fla's Triton kernels + the dynamic hybrid cache force graph breaks). Token COUNT is
  therefore the latency knob — hence the fast (ACTION+EYES, ~1.2s) vs full (SEE/THINK/ACTION/EYES, ~4.5s)
  split in `vlm_service.decide`. For sub-100 ms/tok you'd need a graphing engine (vLLM/SGLang) under WSL2.

## DO NOT REGRESS
- Keep the `101005` 2 s keepalive and sustained drive.
- Keep the frame-stall watchdog and the keyframe-gate / seq-gap drop.
- Telemetry comes as COMPRESSED RAW RTM peer messages — read `m.rawMessage` + inflate, parse NESTED
  `data.battery.percentage`. Do NOT revert to reading `m.text` or top-level `data.percentage`.
- Keep the brain modular (no monolithic omni — it OOMs 32 GB RAM on load).
