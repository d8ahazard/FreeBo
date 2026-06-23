/*
 * eboproto.h - portable, dependency-free codec for the Enabot EBO control protocol.
 *
 * This is a DROP-IN PROTOCOL LIBRARY: given a logical action it produces the exact
 * wire bytes the robot expects, and it parses inbound telemetry/status. It is
 * transport-agnostic on purpose -- it does NOT open sessions, do P2P/DTLS, or speak
 * to ThroughTek/Kalay. You hand the bytes it builds to whatever transport you already
 * have (the RDT reliable channel, avSendIOCtrl, or the cloud RTM socket).
 *
 * Two control planes are covered, because Enabot uses both depending on model/feature:
 *   1. LAN / on-link MAVLink frames carried over the TUTK RDT reliable channel, plus
 *      the avSendIOCtrl stream-control codes. (EBO SE / Air style.)
 *   2. The app's cloud "RTM" JSON command channel ({"id":...,"data":{...}}), recovered
 *      from a live EBO Air 2. (See ../PROTOCOL_NOTES.md "App RTM control protocol".)
 *
 * Scope note (by request): this library is about the *protocol*, not the legacy TUTK
 * transport. If you only have one transport wired up, you still get every command/codec.
 *
 * Portability: C99, no libc beyond <string.h> memcpy/strlen and <stdio.h> snprintf for
 * the JSON builders. No malloc -- every builder writes into a caller-provided buffer and
 * returns the byte count (>=0) or a negative EBO_ERR_*. IEEE-754 float assumed; all
 * multi-byte integers are serialized little-endian explicitly, so it is endian-safe.
 *
 *   #include "eboproto.h"
 *   // build + compile: see eboproto.c (one .c file) and the Makefile.
 *
 * Provenance: byte layouts mirror autobot/robot/frames.py (MAVLink) and
 * autobot/robot/native/ebo_bridge.c (IOCtrl/audio/bridge framing); RTM IDs from the
 * 2026-06-20 capture documented in ../PROTOCOL_NOTES.md.
 */
#ifndef EBOPROTO_H
#define EBOPROTO_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define EBOPROTO_VERSION_MAJOR 1
#define EBOPROTO_VERSION_MINOR 0
#define EBOPROTO_VERSION_STR   "1.0.0"

/* ---- return codes: builders return bytes written (>=0) or one of these (<0) ---- */
#define EBO_OK                0
#define EBO_ERR_BUFFER       -1   /* output buffer too small                       */
#define EBO_ERR_ARG          -2   /* bad/NULL argument                             */
#define EBO_ERR_UNSUPPORTED  -3   /* action not available on this variant/channel  */
#define EBO_ERR_PARSE        -4   /* malformed input while parsing                 */

/* ---- robot variants we model. GENERIC = safest superset (LAN/MAVLink only). ----
 * Confirmed data: SE (upstream LAN bridge) and AIR2 (RTM capture). AIR/PRO are
 * modelled by extension and routed conservatively until confirmed on hardware. */
typedef enum {
    EBO_VARIANT_GENERIC = 0,
    EBO_VARIANT_SE,
    EBO_VARIANT_AIR,
    EBO_VARIANT_AIR2,
    EBO_VARIANT_PRO,
    EBO_VARIANT__COUNT
} ebo_variant_t;

/* ---- which transport a built message must be sent on ---- */
typedef enum {
    EBO_CH_NONE = 0,
    EBO_CH_RDT_MAVLINK,  /* a full MAVLink frame -> RDT reliable channel (bridge kind 0) */
    EBO_CH_AV_IOCTL,     /* avSendIOCtrl payload  -> [u16 io_type LE][data] (bridge kind 1) */
    EBO_CH_AV_AUDIO,     /* outbound audio frame  -> bridge kind 2                          */
    EBO_CH_RTM_JSON      /* UTF-8 JSON command    -> cloud RTM socket                       */
} ebo_channel_t;

/* =====================================================================
 * MAVLink control frames (LAN / RDT plane)  -- mirrors frames.py
 * ===================================================================== */
#define EBO_MAV_STX            0xFE
#define EBO_MAV_MSGID_MOTOR    202
#define EBO_MAV_MSGID_PARAM    229
#define EBO_MAV_MSGID_COMMAND  200
#define EBO_MAV_CRCX_MOTOR     211
#define EBO_MAV_CRCX_PARAM     208
#define EBO_MAV_CRCX_COMMAND   196
#define EBO_CMD_DOCK           40154

/* Largest MAVLink frame this library emits (PARAM_SET): 6 hdr + 39 payload + 2 crc. */
#define EBO_MAV_MAX_FRAME      48

/* Raw motor frame. Convention matches frames.py: ly>0 = forward (negated on the wire).
 * Inputs are clamped to [-1, 1]. buttons is an 8-bit bitmask. */
int ebo_mav_motor(uint8_t *out, size_t cap, float lx, float ly, float rx, float ry, uint8_t buttons);
/* Convenience: forward (+fwd drives ahead) and turn (+turn = right). */
int ebo_mav_drive(uint8_t *out, size_t cap, float forward, float turn);
int ebo_mav_stop (uint8_t *out, size_t cap);

/* PARAM_SET with a 32-byte "group-key" id. ptype defaults to 11 (REAL32) via _set. */
int ebo_mav_param_set(uint8_t *out, size_t cap, const char *group, const char *key,
                      float value, uint8_t ptype);
int ebo_mav_param_set_real(uint8_t *out, size_t cap, const char *group, const char *key, float value);

/* COMMAND (e.g. dock). */
int ebo_mav_command(uint8_t *out, size_t cap, uint16_t command);
int ebo_mav_dock(uint8_t *out, size_t cap);

/* Boolean feature toggles routed through PARAM_SET. */
typedef enum {
    EBO_TOGGLE_EYES = 0,   /* display/enable           */
    EBO_TOGGLE_NIGHT,      /* video/night_vision       */
    EBO_TOGGLE_AVOID,      /* control/auto_avoidance   */
    EBO_TOGGLE_FALL,       /* control/fallarrest       */
    EBO_TOGGLE_PATROL,     /* security_patrol/enable   */
    EBO_TOGGLE_SLEEP,      /* power/sleep (1=sleep,0=wake) */
    EBO_TOGGLE__COUNT
} ebo_toggle_t;
int ebo_mav_toggle(uint8_t *out, size_t cap, ebo_toggle_t which, int on);

/* Eye animation via PARAM_SET (display/expression by default). */
int ebo_mav_eyes_anim(uint8_t *out, size_t cap, int expression_index);

/* Eye-animation name<->index helpers (best-guess map; refine per unit). */
extern const char *const EBO_EYE_ANIM_NAMES[]; /* NULL-terminated, index = wire value */
int  ebo_eye_anim_index(const char *name);     /* -1 if unknown */
const char *ebo_eye_anim_name(int index);      /* NULL if out of range */

/* =====================================================================
 * avSendIOCtrl stream-control plane  -- mirrors ebo_bridge.c
 * ===================================================================== */
#define EBO_IOTYPE_STREAM_START      0x00FF /* request video (4-byte arg)              */
#define EBO_IOTYPE_DEVICE_9930       0x9930 /* device-specific start blob (ioctl9930)  */
#define EBO_IOTYPE_STREAM_SETUP_32A  0x032A
#define EBO_IOTYPE_KEEPALIVE         0x01FF /* re-send ~every 10s                      */
#define EBO_IOTYPE_STREAM_SETUP_9936 0x9936
#define EBO_IOTYPE_AUDIO_START       0x0300 /* IPCAM AUDIOSTART (listen) / SPEAKERSTART */
#define EBO_IOTYPE_SPEAKER_START     0x0300
#define EBO_IOTYPE_AUDIO_DATA        0x0301 /* IPCAM AUDIODATA (talkback fallback)     */

/* Build the talkback audio-data IOCtrl payload: [codec_id][flags=0][len LE16][pcm].
 * codec_id 0x8A = G.711 mu-law @ 8kHz mono (what the bridge tries first). */
int ebo_audio_ioctl_payload(uint8_t *out, size_t cap, uint8_t codec_id, const uint8_t *pcm, int len);
/* Build the 16-byte send frameinfo (fi[0]=codec_id, rest zero) for avSendAudioData. */
int ebo_audio_frameinfo(uint8_t *out, size_t cap, uint8_t codec_id);

/* =====================================================================
 * Bridge pipe framing  -- the [u32 len LE][u8 kind/codec][payload] our own
 * native bridge uses on fd3 (to-native) and stdout (from-native).
 * ===================================================================== */
#define EBO_BRIDGE_KIND_MAVLINK 0
#define EBO_BRIDGE_KIND_IOCTL   1   /* payload = [u16 io_type LE][data] */
#define EBO_BRIDGE_KIND_AUDIO   2   /* payload = [u8 codec_id][G.711]   */
#define EBO_BRIDGE_CODEC_HEVC   80
#define EBO_BRIDGE_CODEC_H264   78
#define EBO_BRIDGE_CODEC_STATUS 0xFF
#define EBO_BRIDGE_CODEC_AUDIO  0xA0

/* Wrap a payload as a bridge frame: [u32 len LE = 1+plen][u8 tag][payload]. */
int ebo_bridge_frame(uint8_t *out, size_t cap, uint8_t tag, const uint8_t *payload, int plen);
/* Wrap an avSendIOCtrl as a kind-1 bridge frame ([u16 io_type LE][data] inside). */
int ebo_bridge_ioctl(uint8_t *out, size_t cap, uint16_t io_type, const uint8_t *data, int dlen);

/* =====================================================================
 * Inbound MAVLink status parsing (from the RDT read / bridge codec 0xFF)
 * ===================================================================== */
typedef struct {
    int has_battery;       int battery_remaining; int charge_state;  /* msgid 207 */
    int has_attitude;      float roll, pitch, yaw;                   /* msgid 30  */
    int has_imu;           int ax, ay, az;                           /* msgid 27  */
} ebo_status_t;
/* Scan a buffer of concatenated MAVLink frames; fill known fields. Returns the
 * number of recognized messages, or negative on bad args. Unknown msgids skipped. */
int ebo_mav_parse_status(const uint8_t *buf, size_t len, ebo_status_t *out);

/* =====================================================================
 * Cloud RTM JSON control plane  -- recovered IDs, see ../PROTOCOL_NOTES.md
 * ===================================================================== */
#define EBO_RTM_DRIVE        101007  /* {lx,ly,rx,ry,buttons}        */
#define EBO_RTM_EMOTE        103003  /* {emojiIds,voiceIds,moveIds,cycleMode} */
#define EBO_RTM_MOVE_MODE    103011  /* {moveMode}                   */
#define EBO_RTM_DOCK         103043  /* (no data)                    */
#define EBO_RTM_AVOID        103045  /* {avoidobstacle:bool}         */
#define EBO_RTM_LASER        103051  /* {laser:bool}                 */
#define EBO_RTM_SHOOT_MODE   102035  /* {shootMode:int}              */
/* connection / status (mostly inbound) */
#define EBO_RTM_LOGIN        101003  /* {userId}                     */
#define EBO_RTM_KEEPALIVE    101005  /* {state}                      */
#define EBO_RTM_ACK          101027
#define EBO_RTM_SLEEP_STATE  101047  /* {isSleeping:bool}            */

/* Context every RTM command carries. sid = session id string from the RTM login;
 * timestamp_ms = epoch milliseconds (0 lets you omit it). */
typedef struct {
    const char *sid;
    uint64_t    timestamp_ms;
} ebo_rtm_ctx_t;

/* RTM command builders -> NUL-terminated JSON in `out`. Return strlen or negative. */
int ebo_rtm_drive(char *out, size_t cap, const ebo_rtm_ctx_t *c,
                  int lx, int ly, int rx, int ry, int buttons);
int ebo_rtm_laser(char *out, size_t cap, const ebo_rtm_ctx_t *c, int on);
int ebo_rtm_avoid(char *out, size_t cap, const ebo_rtm_ctx_t *c, int on);
int ebo_rtm_shoot_mode(char *out, size_t cap, const ebo_rtm_ctx_t *c, int mode);
int ebo_rtm_move_mode(char *out, size_t cap, const ebo_rtm_ctx_t *c, int mode);
int ebo_rtm_dock(char *out, size_t cap, const ebo_rtm_ctx_t *c);
int ebo_rtm_emote(char *out, size_t cap, const ebo_rtm_ctx_t *c,
                  const int *emoji, int n_emoji,
                  const int *voice, int n_voice,
                  const int *move,  int n_move,
                  int cycle_mode);

/* Minimal RTM parse: pulls the integer id and any known data fields it recognizes.
 * Dependency-free scanner (not a general JSON parser); good for the known schema. */
typedef struct {
    long id;
    int has_state;      int state;
    int has_sleeping;   int sleeping;
    int has_laser;      int laser;
    int has_avoid;      int avoid;
    int has_shoot_mode; int shoot_mode;
    int has_move_mode;  int move_mode;
} ebo_rtm_msg_t;
int ebo_rtm_parse(const char *json, ebo_rtm_msg_t *out);

/* =====================================================================
 * One-shot high-level builder: variant + action -> bytes + channel.
 * This is the "do it all in one call" entry point. It consults a per-variant
 * routing table and emits onto the correct channel (LAN MAVLink vs cloud RTM).
 * ===================================================================== */
typedef enum {
    EBO_ACT_DRIVE = 0,   /* params: forward, turn, buttons        */
    EBO_ACT_STOP,
    EBO_ACT_DOCK,
    EBO_ACT_LASER,       /* params: on                            */
    EBO_ACT_AVOID,       /* params: on                            */
    EBO_ACT_NIGHT,       /* params: on                            */
    EBO_ACT_FALL,        /* params: on                            */
    EBO_ACT_PATROL,      /* params: on                            */
    EBO_ACT_EYES,        /* params: on                            */
    EBO_ACT_EYES_ANIM,   /* params: index                         */
    EBO_ACT_SLEEP,       /* params: on (1=sleep, 0=wake)          */
    EBO_ACT_SHOOT_MODE,  /* params: index                         */
    EBO_ACT_MOVE_MODE,   /* params: index                         */
    EBO_ACT_EMOTE,       /* params: emoji/voice/move arrays + cycle_mode */
    EBO_ACT__COUNT
} ebo_action_t;

typedef struct {
    /* drive */
    float    forward, turn;
    uint8_t  buttons;
    /* boolean toggles */
    int      on;
    /* eyes anim / shoot / move mode */
    int      index;
    /* emote composer */
    const int *emoji; int n_emoji;
    const int *voice; int n_voice;
    const int *move;  int n_move;
    int      cycle_mode;
    /* required only when the chosen channel is RTM */
    ebo_rtm_ctx_t rtm;
} ebo_params_t;

typedef struct {
    ebo_channel_t channel; /* where to send `out` */
    int           len;     /* bytes written into `out` (JSON length for RTM) */
} ebo_msg_t;

/* Build one command. `out` receives raw bytes (MAVLink/IOCtrl) or JSON text (RTM).
 * Returns len (>=0) and fills *msg, or a negative EBO_ERR_*. */
int ebo_build(ebo_variant_t v, ebo_action_t act, const ebo_params_t *p,
              uint8_t *out, size_t cap, ebo_msg_t *msg);

/* Introspection: which channel a variant routes an action to (EBO_CH_NONE if N/A). */
ebo_channel_t ebo_route(ebo_variant_t v, ebo_action_t act);
const char   *ebo_variant_name(ebo_variant_t v);
const char   *ebo_channel_name(ebo_channel_t c);
const char   *ebo_action_name(ebo_action_t a);

/* MAVLink X.25 CRC with trailing crc_extra (exposed for tests/advanced use). */
uint16_t ebo_mav_crc(const uint8_t *data, size_t len, uint8_t crc_extra);

#ifdef __cplusplus
}
#endif
#endif /* EBOPROTO_H */
