/* eboproto.c - implementation. See eboproto.h for the contract and provenance. */
#include "eboproto.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

/* ------------------------------------------------------------------ helpers */
static float clampf(float v, float lo, float hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}
static void put_u16le(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put_u32le(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
/* Serialize an IEEE-754 float as 4 little-endian bytes (endian-independent). */
static void put_f32le(uint8_t *p, float f) {
    uint32_t u; memcpy(&u, &f, 4); put_u32le(p, u);
}
static uint16_t rd_u16le(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static float rd_f32le(const uint8_t *p) {
    uint32_t u = (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
    float f; memcpy(&f, &u, 4); return f;
}

/* ------------------------------------------------------------- MAVLink core */
uint16_t ebo_mav_crc(const uint8_t *data, size_t len, uint8_t crc_extra) {
    uint16_t crc = 0xFFFF;
    size_t i;
    for (i = 0; i < len; i++) {
        uint8_t t = (uint8_t)(data[i] ^ (crc & 0xFF));
        t = (uint8_t)(t ^ (t << 4));
        crc = (uint16_t)((crc >> 8) ^ (t << 8) ^ (t << 3) ^ (t >> 4));
    }
    {
        uint8_t t = (uint8_t)(crc_extra ^ (crc & 0xFF));
        t = (uint8_t)(t ^ (t << 4));
        crc = (uint16_t)((crc >> 8) ^ (t << 8) ^ (t << 3) ^ (t >> 4));
    }
    return crc;
}

/* Assemble a MAVLink v1 frame: STX, [len,seq=0,sys=0,comp=0,msgid], payload, crc16 LE. */
static int emit_frame(uint8_t *out, size_t cap, uint8_t msgid,
                      const uint8_t *payload, int plen, uint8_t crc_extra) {
    int total = 1 + 5 + plen + 2;
    uint8_t hdr[5];
    uint16_t crc;
    if (!out || plen < 0) return EBO_ERR_ARG;
    if ((size_t)total > cap) return EBO_ERR_BUFFER;
    hdr[0] = (uint8_t)plen; hdr[1] = 0; hdr[2] = 0; hdr[3] = 0; hdr[4] = msgid;
    out[0] = EBO_MAV_STX;
    memcpy(out + 1, hdr, 5);
    if (plen) memcpy(out + 6, payload, (size_t)plen);
    /* CRC is over hdr(5) + payload, then crc_extra. Build contiguous temp. */
    {
        uint8_t tmp[5 + 64];
        if (plen > 64) return EBO_ERR_BUFFER;
        memcpy(tmp, hdr, 5);
        if (plen) memcpy(tmp + 5, payload, (size_t)plen);
        crc = ebo_mav_crc(tmp, (size_t)(5 + plen), crc_extra);
    }
    put_u16le(out + 6 + plen, crc);
    return total;
}

/* Normalize -0.0 to +0.0 so the wire bytes match the reference builder exactly. */
static float nz(float v) { return v == 0.0f ? 0.0f : v; }

int ebo_mav_motor(uint8_t *out, size_t cap, float lx, float ly, float rx, float ry, uint8_t buttons) {
    uint8_t pl[17];
    put_f32le(pl + 0, nz(clampf(lx, -1.f, 1.f)));
    put_f32le(pl + 4, nz(-clampf(ly, -1.f, 1.f)));   /* ly>0 = forward, negated on the wire */
    put_f32le(pl + 8, nz(clampf(rx, -1.f, 1.f)));
    put_f32le(pl + 12, nz(clampf(ry, -1.f, 1.f)));
    pl[16] = buttons;
    return emit_frame(out, cap, EBO_MAV_MSGID_MOTOR, pl, 17, EBO_MAV_CRCX_MOTOR);
}
int ebo_mav_drive(uint8_t *out, size_t cap, float forward, float turn) {
    return ebo_mav_motor(out, cap, 0.f, forward, turn, 0.f, 0);
}
int ebo_mav_stop(uint8_t *out, size_t cap) {
    return ebo_mav_motor(out, cap, 0.f, 0.f, 0.f, 0.f, 0);
}

int ebo_mav_param_set(uint8_t *out, size_t cap, const char *group, const char *key,
                      float value, uint8_t ptype) {
    uint8_t pl[39];
    char id[33];
    if (!group || !key) return EBO_ERR_ARG;
    /* "group-key" truncated to 32, NUL padded -> matches frames.py. memset first so
     * the bytes past the string (and any unwritten tail) are zero, not stack garbage. */
    memset(id, 0, sizeof id);
    snprintf(id, sizeof id, "%s-%s", group, key);
    memset(pl, 0, sizeof pl);
    put_f32le(pl + 0, value);
    pl[4] = 255; pl[5] = 3;                 /* target_system=255, target_component=3 */
    memcpy(pl + 6, id, 32);                 /* id[] already NUL-padded by snprintf+memset */
    pl[38] = ptype;
    return emit_frame(out, cap, EBO_MAV_MSGID_PARAM, pl, 39, EBO_MAV_CRCX_PARAM);
}
int ebo_mav_param_set_real(uint8_t *out, size_t cap, const char *group, const char *key, float value) {
    return ebo_mav_param_set(out, cap, group, key, value, 11);
}

int ebo_mav_command(uint8_t *out, size_t cap, uint16_t command) {
    uint8_t pl[4];
    put_u16le(pl, command);
    pl[2] = 255; pl[3] = 1;
    return emit_frame(out, cap, EBO_MAV_MSGID_COMMAND, pl, 4, EBO_MAV_CRCX_COMMAND);
}
int ebo_mav_dock(uint8_t *out, size_t cap) { return ebo_mav_command(out, cap, EBO_CMD_DOCK); }

/* toggle -> (group, key). SLEEP uses 1=sleep. */
struct toggle_map { const char *group, *key; };
static const struct toggle_map TOGGLES[EBO_TOGGLE__COUNT] = {
    { "display",         "enable"        }, /* EYES   */
    { "video",           "night_vision"  }, /* NIGHT  */
    { "control",         "auto_avoidance"}, /* AVOID  */
    { "control",         "fallarrest"    }, /* FALL   */
    { "security_patrol", "enable"        }, /* PATROL */
    { "power",           "sleep"         }  /* SLEEP  */
};
int ebo_mav_toggle(uint8_t *out, size_t cap, ebo_toggle_t which, int on) {
    if (which < 0 || which >= EBO_TOGGLE__COUNT) return EBO_ERR_ARG;
    return ebo_mav_param_set_real(out, cap, TOGGLES[which].group, TOGGLES[which].key, on ? 1.f : 0.f);
}

/* ------------------------------------------------------------- eye anims */
const char *const EBO_EYE_ANIM_NAMES[] = {
    "neutral", "happy", "sad", "angry", "surprised", "sleepy", "love", "dizzy", "blink", 0
};
int ebo_eye_anim_index(const char *name) {
    int i;
    if (!name) return -1;
    for (i = 0; EBO_EYE_ANIM_NAMES[i]; i++)
        if (strcmp(EBO_EYE_ANIM_NAMES[i], name) == 0) return i;
    return -1;
}
const char *ebo_eye_anim_name(int index) {
    int i;
    if (index < 0) return 0;
    for (i = 0; EBO_EYE_ANIM_NAMES[i]; i++)
        if (i == index) return EBO_EYE_ANIM_NAMES[i];
    return 0;
}
int ebo_mav_eyes_anim(uint8_t *out, size_t cap, int expression_index) {
    return ebo_mav_param_set_real(out, cap, "display", "expression", (float)expression_index);
}

/* ------------------------------------------------------------- audio / ioctl */
int ebo_audio_ioctl_payload(uint8_t *out, size_t cap, uint8_t codec_id, const uint8_t *pcm, int len) {
    if (!out || len < 0 || (len && !pcm)) return EBO_ERR_ARG;
    if ((size_t)(4 + len) > cap) return EBO_ERR_BUFFER;
    out[0] = codec_id; out[1] = 0;
    put_u16le(out + 2, (uint16_t)len);
    if (len) memcpy(out + 4, pcm, (size_t)len);
    return 4 + len;
}
int ebo_audio_frameinfo(uint8_t *out, size_t cap, uint8_t codec_id) {
    if (!out) return EBO_ERR_ARG;
    if (cap < 16) return EBO_ERR_BUFFER;
    memset(out, 0, 16);
    out[0] = codec_id;
    return 16;
}

/* ------------------------------------------------------------- bridge framing */
int ebo_bridge_frame(uint8_t *out, size_t cap, uint8_t tag, const uint8_t *payload, int plen) {
    if (!out || plen < 0 || (plen && !payload)) return EBO_ERR_ARG;
    if ((size_t)(5 + plen) > cap) return EBO_ERR_BUFFER;
    put_u32le(out, (uint32_t)(1 + plen));
    out[4] = tag;
    if (plen) memcpy(out + 5, payload, (size_t)plen);
    return 5 + plen;
}
int ebo_bridge_ioctl(uint8_t *out, size_t cap, uint16_t io_type, const uint8_t *data, int dlen) {
    /* payload = [u16 io_type LE][data]; wrapped as kind-1 bridge frame */
    if (!out || dlen < 0 || (dlen && !data)) return EBO_ERR_ARG;
    if ((size_t)(5 + 2 + dlen) > cap) return EBO_ERR_BUFFER;
    put_u32le(out, (uint32_t)(1 + 2 + dlen));
    out[4] = EBO_BRIDGE_KIND_IOCTL;
    put_u16le(out + 5, io_type);
    if (dlen) memcpy(out + 7, data, (size_t)dlen);
    return 7 + dlen;
}

/* ------------------------------------------------------------- inbound parse */
int ebo_mav_parse_status(const uint8_t *buf, size_t len, ebo_status_t *out) {
    size_t i = 0;
    int found = 0;
    if (!buf || !out) return EBO_ERR_ARG;
    memset(out, 0, sizeof *out);
    while (i + 8 <= len) {
        if (buf[i] != EBO_MAV_STX) { i++; continue; }
        {
            int plen = buf[i + 1];
            int msgid = buf[i + 5];
            size_t p = i + 6; /* payload start */
            if (msgid == 207 && i + 24 <= len) {            /* BATTERY_STATUS */
                out->has_battery = 1;
                out->battery_remaining = buf[i + 22];
                out->charge_state = buf[i + 23];
                found++;
            } else if (msgid == 30 && p + 16 <= len) {       /* ATTITUDE */
                out->has_attitude = 1;
                out->roll  = rd_f32le(buf + p + 4);
                out->pitch = rd_f32le(buf + p + 8);
                out->yaw   = rd_f32le(buf + p + 12);
                found++;
            } else if (msgid == 27 && p + 14 <= len) {       /* RAW_IMU */
                out->has_imu = 1;
                out->ax = (int16_t)rd_u16le(buf + p + 8);
                out->ay = (int16_t)rd_u16le(buf + p + 10);
                out->az = (int16_t)rd_u16le(buf + p + 12);
                found++;
            }
            i += plen ? (size_t)(8 + plen + 2) : 1;
        }
    }
    return found;
}

/* ------------------------------------------------------------- RTM builders */
static const char *rtm_sid(const ebo_rtm_ctx_t *c) { return (c && c->sid) ? c->sid : ""; }
static uint64_t    rtm_ts (const ebo_rtm_ctx_t *c) { return c ? c->timestamp_ms : 0; }

static int chk(int n, size_t cap) { return (n < 0 || (size_t)n >= cap) ? EBO_ERR_BUFFER : n; }

int ebo_rtm_drive(char *out, size_t cap, const ebo_rtm_ctx_t *c,
                  int lx, int ly, int rx, int ry, int buttons) {
    int n;
    if (!out) return EBO_ERR_ARG;
    n = snprintf(out, cap,
        "{\"id\":%d,\"sid\":\"%s\",\"data\":{\"lx\":%d,\"ly\":%d,\"rx\":%d,\"ry\":%d,\"buttons\":%d},"
        "\"type\":0,\"timestamp\":%llu}",
        EBO_RTM_DRIVE, rtm_sid(c), lx, ly, rx, ry, buttons, (unsigned long long)rtm_ts(c));
    return chk(n, cap);
}
static int rtm_bool(char *out, size_t cap, const ebo_rtm_ctx_t *c, int id, const char *key, int v) {
    int n = snprintf(out, cap,
        "{\"id\":%d,\"sid\":\"%s\",\"data\":{\"%s\":%s},\"type\":0,\"timestamp\":%llu}",
        id, rtm_sid(c), key, v ? "true" : "false", (unsigned long long)rtm_ts(c));
    return chk(n, cap);
}
static int rtm_int(char *out, size_t cap, const ebo_rtm_ctx_t *c, int id, const char *key, int v) {
    int n = snprintf(out, cap,
        "{\"id\":%d,\"sid\":\"%s\",\"data\":{\"%s\":%d},\"type\":0,\"timestamp\":%llu}",
        id, rtm_sid(c), key, v, (unsigned long long)rtm_ts(c));
    return chk(n, cap);
}
int ebo_rtm_laser(char *out, size_t cap, const ebo_rtm_ctx_t *c, int on) {
    if (!out) return EBO_ERR_ARG;
    return rtm_bool(out, cap, c, EBO_RTM_LASER, "laser", on);
}
int ebo_rtm_avoid(char *out, size_t cap, const ebo_rtm_ctx_t *c, int on) {
    if (!out) return EBO_ERR_ARG;
    return rtm_bool(out, cap, c, EBO_RTM_AVOID, "avoidobstacle", on);
}
int ebo_rtm_shoot_mode(char *out, size_t cap, const ebo_rtm_ctx_t *c, int mode) {
    if (!out) return EBO_ERR_ARG;
    return rtm_int(out, cap, c, EBO_RTM_SHOOT_MODE, "shootMode", mode);
}
int ebo_rtm_move_mode(char *out, size_t cap, const ebo_rtm_ctx_t *c, int mode) {
    if (!out) return EBO_ERR_ARG;
    return rtm_int(out, cap, c, EBO_RTM_MOVE_MODE, "moveMode", mode);
}
int ebo_rtm_dock(char *out, size_t cap, const ebo_rtm_ctx_t *c) {
    int n;
    if (!out) return EBO_ERR_ARG;
    n = snprintf(out, cap, "{\"id\":%d,\"sid\":\"%s\",\"type\":0,\"timestamp\":%llu}",
                 EBO_RTM_DOCK, rtm_sid(c), (unsigned long long)rtm_ts(c));
    return chk(n, cap);
}
static int write_int_array(char *buf, size_t cap, const int *a, int n) {
    size_t off = 0;
    int i, w;
    if (cap < 3) return EBO_ERR_BUFFER;
    buf[off++] = '[';
    for (i = 0; i < n; i++) {
        w = snprintf(buf + off, cap - off, "%s%d", i ? "," : "", a[i]);
        if (w < 0 || (size_t)w >= cap - off) return EBO_ERR_BUFFER;
        off += (size_t)w;
    }
    if (off + 2 > cap) return EBO_ERR_BUFFER;
    buf[off++] = ']'; buf[off] = '\0';
    return (int)off;
}
int ebo_rtm_emote(char *out, size_t cap, const ebo_rtm_ctx_t *c,
                  const int *emoji, int n_emoji, const int *voice, int n_voice,
                  const int *move, int n_move, int cycle_mode) {
    char ae[128], av[128], am[128];
    int n;
    if (!out) return EBO_ERR_ARG;
    if (write_int_array(av, sizeof av, voice, n_voice) < 0) return EBO_ERR_BUFFER;
    if (write_int_array(ae, sizeof ae, emoji, n_emoji) < 0) return EBO_ERR_BUFFER;
    if (write_int_array(am, sizeof am, move,  n_move)  < 0) return EBO_ERR_BUFFER;
    n = snprintf(out, cap,
        "{\"id\":%d,\"sid\":\"%s\",\"data\":{\"voiceIds\":%s,\"cycleMode\":%d,\"emojiIds\":%s,\"moveIds\":%s},"
        "\"type\":0,\"timestamp\":%llu}",
        EBO_RTM_EMOTE, rtm_sid(c), av, cycle_mode, ae, am, (unsigned long long)rtm_ts(c));
    return chk(n, cap);
}

/* ------------------------------------------------------------- RTM parse */
/* Return pointer just past `"key":` (skipping spaces), or NULL. */
static const char *find_value(const char *json, const char *key) {
    size_t klen = strlen(key);
    const char *s = json;
    char pat[64];
    if (klen + 3 >= sizeof pat) return 0;
    pat[0] = '"';
    memcpy(pat + 1, key, klen);
    pat[1 + klen] = '"';
    pat[2 + klen] = ':';
    pat[3 + klen] = '\0';
    s = strstr(json, pat);
    if (!s) return 0;
    s += klen + 3;
    while (*s == ' ' || *s == '\t') s++;
    return s;
}
static int get_long(const char *json, const char *key, long *out) {
    const char *v = find_value(json, key);
    char *end;
    long val;
    if (!v) return 0;
    val = (long)strtol(v, &end, 10);
    if (end == v) return 0;
    *out = val; return 1;
}
static int get_bool(const char *json, const char *key, int *out) {
    const char *v = find_value(json, key);
    if (!v) return 0;
    if (v[0] == 't') { *out = 1; return 1; }
    if (v[0] == 'f') { *out = 0; return 1; }
    if (v[0] == '1') { *out = 1; return 1; }
    if (v[0] == '0') { *out = 0; return 1; }
    return 0;
}
int ebo_rtm_parse(const char *json, ebo_rtm_msg_t *out) {
    long lv;
    if (!json || !out) return EBO_ERR_ARG;
    memset(out, 0, sizeof *out);
    if (!get_long(json, "id", &out->id)) return EBO_ERR_PARSE;
    if (get_long(json, "state", &lv))      { out->has_state = 1;      out->state = (int)lv; }
    if (get_bool(json, "isSleeping", &out->sleeping)) out->has_sleeping = 1;
    if (get_bool(json, "laser", &out->laser))          out->has_laser = 1;
    if (get_bool(json, "avoidobstacle", &out->avoid))  out->has_avoid = 1;
    if (get_long(json, "shootMode", &lv)) { out->has_shoot_mode = 1; out->shoot_mode = (int)lv; }
    if (get_long(json, "moveMode", &lv))  { out->has_move_mode = 1;  out->move_mode = (int)lv; }
    return EBO_OK;
}

/* ------------------------------------------------------------- routing table */
/* For each variant, which channel handles each action (EBO_CH_NONE = unsupported). */
ebo_channel_t ebo_route(ebo_variant_t v, ebo_action_t act) {
    int rtm_centric = (v == EBO_VARIANT_AIR2 || v == EBO_VARIANT_PRO);
    switch (act) {
        case EBO_ACT_DRIVE:
        case EBO_ACT_STOP:      return rtm_centric ? EBO_CH_RTM_JSON : EBO_CH_RDT_MAVLINK;
        case EBO_ACT_DOCK:      return rtm_centric ? EBO_CH_RTM_JSON : EBO_CH_RDT_MAVLINK;
        case EBO_ACT_AVOID:     return rtm_centric ? EBO_CH_RTM_JSON : EBO_CH_RDT_MAVLINK;
        case EBO_ACT_LASER:     return rtm_centric ? EBO_CH_RTM_JSON : EBO_CH_NONE; /* no LAN laser known */
        case EBO_ACT_SHOOT_MODE:return rtm_centric ? EBO_CH_RTM_JSON : EBO_CH_NONE;
        case EBO_ACT_MOVE_MODE: return rtm_centric ? EBO_CH_RTM_JSON : EBO_CH_NONE;
        case EBO_ACT_EMOTE:     return rtm_centric ? EBO_CH_RTM_JSON : EBO_CH_NONE;
        case EBO_ACT_EYES_ANIM: return rtm_centric ? EBO_CH_RTM_JSON : EBO_CH_RDT_MAVLINK;
        /* These were confirmed OFF the RTM channel on Air 2 -> always LAN/param. */
        case EBO_ACT_NIGHT:
        case EBO_ACT_PATROL:
        case EBO_ACT_FALL:
        case EBO_ACT_EYES:
        case EBO_ACT_SLEEP:     return EBO_CH_RDT_MAVLINK;
        default:                return EBO_CH_NONE;
    }
}

int ebo_build(ebo_variant_t v, ebo_action_t act, const ebo_params_t *p,
              uint8_t *out, size_t cap, ebo_msg_t *msg) {
    ebo_channel_t ch;
    int n = EBO_ERR_UNSUPPORTED;
    ebo_params_t z;
    if (!out || !msg) return EBO_ERR_ARG;
    if (!p) { memset(&z, 0, sizeof z); p = &z; }
    ch = ebo_route(v, act);
    msg->channel = ch;
    msg->len = 0;
    if (ch == EBO_CH_NONE) return EBO_ERR_UNSUPPORTED;

    if (ch == EBO_CH_RDT_MAVLINK) {
        switch (act) {
            case EBO_ACT_DRIVE:     n = ebo_mav_drive(out, cap, p->forward, p->turn); break;
            case EBO_ACT_STOP:      n = ebo_mav_stop(out, cap); break;
            case EBO_ACT_DOCK:      n = ebo_mav_dock(out, cap); break;
            case EBO_ACT_AVOID:     n = ebo_mav_toggle(out, cap, EBO_TOGGLE_AVOID,  p->on); break;
            case EBO_ACT_NIGHT:     n = ebo_mav_toggle(out, cap, EBO_TOGGLE_NIGHT,  p->on); break;
            case EBO_ACT_FALL:      n = ebo_mav_toggle(out, cap, EBO_TOGGLE_FALL,   p->on); break;
            case EBO_ACT_PATROL:    n = ebo_mav_toggle(out, cap, EBO_TOGGLE_PATROL, p->on); break;
            case EBO_ACT_EYES:      n = ebo_mav_toggle(out, cap, EBO_TOGGLE_EYES,   p->on); break;
            case EBO_ACT_SLEEP:     n = ebo_mav_toggle(out, cap, EBO_TOGGLE_SLEEP,  p->on); break;
            case EBO_ACT_EYES_ANIM: n = ebo_mav_eyes_anim(out, cap, p->index); break;
            default:                n = EBO_ERR_UNSUPPORTED; break;
        }
    } else if (ch == EBO_CH_RTM_JSON) {
        char *j = (char *)out;
        switch (act) {
            case EBO_ACT_DRIVE: {
                /* RTM drive uses integer -100..100; map forward/turn floats to that range. */
                int ly = (int)(clampf(p->forward, -1.f, 1.f) * 100.f);
                int rx = (int)(clampf(p->turn,    -1.f, 1.f) * 100.f);
                n = ebo_rtm_drive(j, cap, &p->rtm, 0, ly, rx, 0, p->buttons);
            } break;
            case EBO_ACT_STOP:       n = ebo_rtm_drive(j, cap, &p->rtm, 0, 0, 0, 0, 0); break;
            case EBO_ACT_DOCK:       n = ebo_rtm_dock(j, cap, &p->rtm); break;
            case EBO_ACT_LASER:      n = ebo_rtm_laser(j, cap, &p->rtm, p->on); break;
            case EBO_ACT_AVOID:      n = ebo_rtm_avoid(j, cap, &p->rtm, p->on); break;
            case EBO_ACT_SHOOT_MODE: n = ebo_rtm_shoot_mode(j, cap, &p->rtm, p->index); break;
            case EBO_ACT_MOVE_MODE:  n = ebo_rtm_move_mode(j, cap, &p->rtm, p->index); break;
            case EBO_ACT_EYES_ANIM: {
                int one = p->index;
                n = ebo_rtm_emote(j, cap, &p->rtm, &one, 1, 0, 0, 0, 0, 0);
            } break;
            case EBO_ACT_EMOTE:
                n = ebo_rtm_emote(j, cap, &p->rtm, p->emoji, p->n_emoji, p->voice, p->n_voice,
                                  p->move, p->n_move, p->cycle_mode);
                break;
            default: n = EBO_ERR_UNSUPPORTED; break;
        }
    }
    if (n < 0) return n;
    msg->len = n;
    return n;
}

/* ------------------------------------------------------------- introspection */
const char *ebo_variant_name(ebo_variant_t v) {
    switch (v) {
        case EBO_VARIANT_GENERIC: return "generic";
        case EBO_VARIANT_SE:      return "ebo-se";
        case EBO_VARIANT_AIR:     return "ebo-air";
        case EBO_VARIANT_AIR2:    return "ebo-air2";
        case EBO_VARIANT_PRO:     return "ebo-pro";
        default:                  return "?";
    }
}
const char *ebo_channel_name(ebo_channel_t c) {
    switch (c) {
        case EBO_CH_NONE:        return "none";
        case EBO_CH_RDT_MAVLINK: return "rdt-mavlink";
        case EBO_CH_AV_IOCTL:    return "av-ioctl";
        case EBO_CH_AV_AUDIO:    return "av-audio";
        case EBO_CH_RTM_JSON:    return "rtm-json";
        default:                 return "?";
    }
}
const char *ebo_action_name(ebo_action_t a) {
    static const char *N[EBO_ACT__COUNT] = {
        "drive", "stop", "dock", "laser", "avoid", "night", "fall", "patrol",
        "eyes", "eyes_anim", "sleep", "shoot_mode", "move_mode", "emote"
    };
    if (a < 0 || a >= EBO_ACT__COUNT) return "?";
    return N[a];
}
