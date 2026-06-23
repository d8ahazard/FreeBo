# eboproto — portable Enabot EBO control-protocol codec

A single, dependency-free C library that turns logical robot actions into the exact
wire bytes an Enabot EBO expects, and parses inbound telemetry/status. Drop the two
files (`eboproto.h` + `eboproto.c`) into anything and compile — no external deps, no
malloc, endian-safe C99. Builds as a static lib, shared lib, or straight into your TU.

It is a **codec, not a transport**. It does *not* do TUTK/Kalay P2P, DTLS, sessions, or
sockets. You already have (or will write) the transport; this library gives you every
command and parser so you don't have to re-reverse the protocol per project.

## Why two control planes

Enabot models split control across two channels, so this library builds both:

| Plane | Channel enum | What it carries |
|-------|--------------|-----------------|
| LAN / on-link | `EBO_CH_RDT_MAVLINK` | MAVLink frames over the TUTK RDT reliable channel (drive, dock, param toggles, eyes) |
| LAN / stream | `EBO_CH_AV_IOCTL`, `EBO_CH_AV_AUDIO` | `avSendIOCtrl` stream-control + outbound audio framing |
| Cloud | `EBO_CH_RTM_JSON` | the app's `{"id":...,"data":{...}}` RTM JSON commands |

The MAVLink layouts are byte-identical to `autobot/robot/frames.py` (verified by golden
vectors in the self-test). The RTM IDs were recovered from a live EBO Air 2 — see
[`../PROTOCOL_NOTES.md`](../PROTOCOL_NOTES.md) → "App RTM control protocol".

## Build

```sh
make            # static lib (libeboproto.a) + self-test
make test       # build & run the golden-vector self-test
make shared     # libeboproto.so (or .dll on MinGW)
```

MSVC:

```bat
cl /std:c11 /TC eboproto.c eboproto_selftest.c /Fe:selftest.exe && selftest.exe
```

Android NDK (same target the bridge uses):

```sh
clang --target=aarch64-linux-android24 -O2 -c eboproto.c -o eboproto.o
```

All builders write into a caller buffer and return the byte count, or a negative
`EBO_ERR_*`. Nothing allocates; safe on bare metal / MCUs.

## Variant support & routing

`ebo_route(variant, action)` reports which channel an action uses; `ebo_build()` does it
for you in one call. Confirmed from real units: **SE** (LAN MAVLink) and **Air 2** (cloud
RTM for drive/laser/avoid/dock/shoot/move/emote; LAN param for night/patrol/fall/eyes/sleep).
`GENERIC`/`AIR`/`PRO` route conservatively until confirmed on hardware.

| action | SE / AIR | AIR2 / PRO |
|--------|----------|------------|
| drive / stop / dock / avoid | MAVLink | RTM |
| eyes_anim | MAVLink (`display/expression`) | RTM (`emojiIds`) |
| laser / shoot_mode / move_mode / emote | — (unsupported) | RTM |
| night / patrol / fall / eyes / sleep | MAVLink param | MAVLink param |

## API at a glance

```c
#include "eboproto.h"

uint8_t buf[64];
ebo_msg_t msg;
ebo_params_t p = {0};

/* One-shot: pick variant + action, get bytes + the channel to send them on. */
p.forward = 1.0f;                       /* full speed ahead */
p.rtm.sid = session_id;                 /* only needed if the route is RTM   */
int n = ebo_build(EBO_VARIANT_AIR2, EBO_ACT_DRIVE, &p, buf, sizeof buf, &msg);
/* msg.channel == EBO_CH_RTM_JSON; buf holds JSON of length n -> send on RTM socket */

/* Or call the layer directly: */
n = ebo_mav_dock(buf, sizeof buf);                  /* MAVLink dock frame      */
n = ebo_mav_toggle(buf, sizeof buf, EBO_TOGGLE_NIGHT, 1);
n = ebo_rtm_laser(json, sizeof json, &ctx, 1);      /* cloud laser-on JSON     */

/* Parse inbound. */
ebo_status_t st; ebo_mav_parse_status(rdt_bytes, len, &st);   /* battery/imu/attitude */
ebo_rtm_msg_t m; ebo_rtm_parse(json_in, &m);                  /* id + known fields    */
```

### Layers

- **MAVLink builders:** `ebo_mav_motor/drive/stop`, `ebo_mav_param_set[_real]`,
  `ebo_mav_command/dock`, `ebo_mav_toggle`, `ebo_mav_eyes_anim`.
- **Stream/audio:** IOTYPE constants, `ebo_audio_ioctl_payload`, `ebo_audio_frameinfo`.
- **Bridge framing:** `ebo_bridge_frame`, `ebo_bridge_ioctl` (the `[u32 len][u8 tag]` pipe
  format used by the native bridge — handy if you reuse that supervisor).
- **RTM builders:** `ebo_rtm_drive/laser/avoid/shoot_mode/move_mode/dock/emote`.
- **Parsers:** `ebo_mav_parse_status`, `ebo_rtm_parse`.
- **Routing/introspection:** `ebo_build`, `ebo_route`, `ebo_*_name`.

## Scope / non-goals

- No TUTK/Kalay transport, no DTLS-PSK, no session setup. (Out of scope by design.)
- The cloud RTM *transport* (websocket/auth) is not implemented — only the message codec.
- `AIR`/`PRO` routing is a conservative model; confirm per unit and refine
  `ebo_route()` + `../PROTOCOL_NOTES.md` as you verify.

## License

Same terms as the surrounding `ebo-se-lan-bridge` project (MIT). Protocol facts derived
from clean-room reverse engineering of the user's own device.
