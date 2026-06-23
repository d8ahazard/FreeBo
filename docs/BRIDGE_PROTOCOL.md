# Robot protocol

This is the robot-facing contract, all inside the one app. Two layers:

1. the **native ⇄ NativeRobotLink** pipe protocol (process-to-process, inside the app), and
2. the **RobotLink verbs** (what the brain calls in-process).

Keep this document in sync with `autobot/robot/native/ebo_bridge.c`, `autobot/robot/native_link.py`, and
`autobot/robot/frames.py`.

## 1. Native ⇄ NativeRobotLink (stdio + fd3)

The native `ebo_bridge` process is spawned by `NativeRobotLink`. They exchange length-prefixed frames.

### Native → NativeRobotLink (the native process's stdout)

```
[u32 len LE][u8 codec][payload …]   (len = 1 + payload size)
```

| codec | meaning | payload |
|-------|---------|---------|
| 80 | HEVC video frame | Annex-B H.265 |
| 78 | H.264 video frame | Annex-B H.264 |
| 0xFF | inbound MAVLink status | raw MAVLink bytes (battery etc.) |
| 0xA0 | inbound audio (listen) | `[u8 codec_id][u8 flags][G.711 data]` |

### NativeRobotLink → native (the native process's fd 3)

```
[u32 len LE][u8 kind][payload …]   (len = 1 + payload size)
```

| kind | meaning | payload |
|------|---------|---------|
| 0 | MAVLink over RDT | a full MAVLink frame (motor/dock/param/eyes) |
| 1 | avSendIOCtrl | `[u16 io_type LE][data]` |
| 2 | **outbound audio (talkback)** — Autobot addition | `[u8 codec_id][G.711 data]` |

**Talkback (kind 2):** the native side accumulates these and calls the TUTK speaker-send path. On connect it
issues `IOTYPE_USER_IPCAM_SPEAKERSTART (0x300)` and, if present, `avSendAudioData`. The exact codec and call
shape are device/SDK-specific; the native code tries the documented G.711 µ-law path (`codec_id 0x8a`,
8 kHz mono) and logs the result. If the symbol/path is unavailable it logs `talkback unavailable` and drops
the audio — see the **Talkback status** note below.

## 2. MAVLink frame builders (single source of truth: `autobot/robot/frames.py`)

- `motor_frame(ly, rx, lx=0, ry=0, buttons=0)` — msgid 202, crc_extra 211. Floats `lx,-ly,rx,ry` + 1 byte
  buttons. `ly>0` = forward (negated to match the robot's convention).
- `param_set_frame(group, key, value, ptype=11)` — msgid 229, crc_extra 208. PARAM_SET with a 32-byte
  `"group-key"` id. Used for toggles and eye animations.
- `command_frame(command)` — msgid 200, crc_extra 196. `CMD_DOCK = 40154`.

### Known params (`group/key → meaning`)

| name | group/key | values |
|------|-----------|--------|
| eyes on/off | `display/enable` | 0/1 |
| eye animation | `display/expression` | enum index (probed) — see Eye animations |
| night vision | `video/night_vision` | 0/1 |
| collision avoidance | `control/auto_avoidance` | 0/1 |
| fall protection | `control/fallarrest` | 0/1 |
| patrol | `security_patrol/enable` | 0/1 |
| sleep/wake | `power/sleep` | 1=sleep, 0=wake |

### Inbound status parsing

`BATTERY_STATUS` is msgid 207; `NativeRobotLink` reads `battery_remaining` and `charge_state` bytes. It also
opportunistically parses `ATTITUDE` (msgid 30 → roll/pitch/yaw) and `RAW_IMU` (msgid 27 → accel) if the
firmware streams them, surfacing `attitude`/`imu` in telemetry. Any other inbound msgid is **logged once**
(`inbound MAVLink msgid=… (unhandled — discovery)`) so accelerometer/other telemetry can be identified per
firmware. Other
inbound MAVLink is logged for discovery (used to map telemetry + eye animations).

## 3. RobotLink verbs (the brain calls these in-process)

`autobot/robot/link.py` defines the contract; `NativeRobotLink`/`MockRobotLink` implement it. All are async
and return JSON-able dicts (fail-soft: `{"ok": False, ...}` rather than raising).

| method | args | returns |
|--------|------|---------|
| `info()` | — | `{connected, paused, awake, codec, frames_received, audio, battery, charge, rtsp}` |
| `telemetry()` | — | structured telemetry (superset of `info`, plus eyes, toggles, audio in/out) |
| `snapshot()` | — | `(jpeg_bytes, error)`; `(None, reason)` while asleep / no frame yet |
| `move(ly, rx, duration)` | — | `{ok}` — timed move then auto-stop |
| `drive(ly, rx)` | — | `{ok}` — one continuous frame; the native deadman stops on silence |
| `stop()` | — | `{ok}` |
| `action(name)` | — | `{ok, action}` — see action names below |
| `say_audio(g711, codec)` / `say_text(text)` | — | `{ok, frames, available}` — talkback (text path uses local TTS) |
| `connection(state)` | `start\|stop` | `{ok, paused}` — release/reclaim the robot for the Enabot app |

The browser does not call these directly. `autobot/web/server.py` exposes the HTTP/WebSocket surface the UI
uses (`/api/state`, `/api/control`, `/api/estop`, `/api/tick`, `/api/snapshot.jpg`, `/whep`, `/hls/...`,
`/ws`) and translates `/api/control` actions into `RobotLink` calls (through the safety floor).

### action names

`wake`, `sleep`, `dock`, `undock`, `forward`, `backward`, `left`, `right`,
`eyes_on`, `eyes_off`, `night_on`, `night_off`, `avoid_on`, `avoid_off`, `fall_on`, `fall_off`,
`patrol_on`, `patrol_off`, and eye animations `eyes_<anim>` (e.g. `eyes_happy`) — see below.

## Eye animations (bonus feature + discovery)

The upstream bridge only had eyes on/off (`display/enable`). Autobot adds **eye animation** control:

- We send `param_set_frame("display", "expression", <index>)` (and try `"display"/"eye"`/`"emoji"` as
  alternates) and observe the robot. `autobot/robot/frames.py` defines `EYE_ANIMATIONS` as a name→index map
  that starts as a best-guess and is refined by the discovery script.
- `autobot/brain/tools.py` exposes `set_eyes(animation)` over the discovered set; if discovery found nothing,
  the set collapses to `on`/`off` so the feature degrades gracefully.
- The discovery helper: `autobot/robot/native/probe_eyes.py` POSTs candidate group/key/value combinations to
  the running app's `/api/debug/param`, with a pause between each so you can watch the robot.

## Talkback status

Talkback is **attempted in v1**. The native send path depends on which TUTK symbols your `.so` set exports
(`avSendAudioData` vs an IOCTL-based path) and the codec the EBO firmware accepts. The native code:

1. sends `SPEAKERSTART (0x300)` on connect,
2. on each `kind 2` frame, calls `avSendAudioData` if the symbol resolved, else falls back to
   `avSendIOCtrl(IOTYPE_USER_IPCAM_AUDIODATA)`,
3. logs `talkback: <path> rc=<n>` so you can see what worked.

If neither path engages on your unit, talkback degrades to a no-op and the UI shows "talkback unavailable".
The PC side (TTS + `/say` + UI toggle) is fully built regardless, so finishing talkback is a native-only change.
