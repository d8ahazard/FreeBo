/* eboproto_selftest.c - golden-vector + round-trip tests.
 *
 * The MAVLink golden vectors are byte-for-byte output of autobot/robot/frames.py
 * (the authoritative Python builder), so this proves the C port is identical.
 *
 *   cc -std=c99 -Wall -Wextra eboproto.c eboproto_selftest.c -o selftest && ./selftest
 */
#include "eboproto.h"
#include <stdio.h>
#include <string.h>

static int g_fail = 0;

static void hex(char *dst, const uint8_t *b, int n) {
    static const char *H = "0123456789abcdef";
    int i;
    for (i = 0; i < n; i++) { dst[2*i] = H[b[i] >> 4]; dst[2*i+1] = H[b[i] & 0xF]; }
    dst[2*n] = '\0';
}

static void expect_hex(const char *name, const uint8_t *got, int n, const char *want) {
    char buf[256];
    if (n < 0) { printf("FAIL %-12s builder returned error %d\n", name, n); g_fail++; return; }
    hex(buf, got, n);
    if (strcmp(buf, want) == 0) {
        printf("ok   %-12s %s\n", name, buf);
    } else {
        printf("FAIL %-12s\n       got=%s\n      want=%s\n", name, buf, want);
        g_fail++;
    }
}
static void expect_str(const char *name, const char *got, int n, const char *want) {
    if (n < 0) { printf("FAIL %-12s builder returned error %d\n", name, n); g_fail++; return; }
    if (strcmp(got, want) == 0) printf("ok   %-12s %s\n", name, got);
    else { printf("FAIL %-12s\n       got=%s\n      want=%s\n", name, got, want); g_fail++; }
}
static void expect_int(const char *name, long got, long want) {
    if (got == want) printf("ok   %-12s %ld\n", name, got);
    else { printf("FAIL %-12s got=%ld want=%ld\n", name, got, want); g_fail++; }
}

int main(void) {
    uint8_t b[256];
    char j[512];
    int n;

    printf("eboproto %s self-test\n\n", EBOPROTO_VERSION_STR);
    printf("-- MAVLink golden vectors (vs frames.py) --\n");
    n = ebo_mav_motor(b, sizeof b, 0, 0, 0, 0, 0);
    expect_hex("motor_stop", b, n, "fe11000000ca0000000000000000000000000000000000654f");
    n = ebo_mav_motor(b, sizeof b, 0.f, 1.0f, 0.f, 0.f, 0);
    expect_hex("motor_fwd", b, n, "fe11000000ca00000000000080bf0000000000000000003dfc");
    n = ebo_mav_motor(b, sizeof b, 0.1f, 0.5f, -0.25f, 0.f, 3);
    expect_hex("motor_fl", b, n, "fe11000000cacdcccc3d000000bf000080be0000000003a367");
    n = ebo_mav_dock(b, sizeof b);
    expect_hex("dock", b, n, "fe04000000c8da9cff01579f");
    n = ebo_mav_toggle(b, sizeof b, EBO_TOGGLE_EYES, 1);
    expect_hex("eyes_on", b, n,
        "fe27000000e50000803fff03646973706c61792d656e61626c650000000000000000000000000000000000000b637b");
    n = ebo_mav_toggle(b, sizeof b, EBO_TOGGLE_SLEEP, 1);
    expect_hex("sleep", b, n,
        "fe27000000e50000803fff03706f7765722d736c6565700000000000000000000000000000000000000000000b4c8d");
    n = ebo_mav_eyes_anim(b, sizeof b, 1);
    expect_hex("eyes_happy", b, n,
        "fe27000000e50000803fff03646973706c61792d65787072657373696f6e00000000000000000000000000000bb650");

    printf("\n-- RTM JSON builders --\n");
    {
        ebo_rtm_ctx_t c; c.sid = "fLaBO4gua8WF7v2u"; c.timestamp_ms = 0;
        n = ebo_rtm_laser(j, sizeof j, &c, 1);
        expect_str("rtm_laser", j, n,
            "{\"id\":103051,\"sid\":\"fLaBO4gua8WF7v2u\",\"data\":{\"laser\":true},\"type\":0,\"timestamp\":0}");
        n = ebo_rtm_avoid(j, sizeof j, &c, 0);
        expect_str("rtm_avoid", j, n,
            "{\"id\":103045,\"sid\":\"fLaBO4gua8WF7v2u\",\"data\":{\"avoidobstacle\":false},\"type\":0,\"timestamp\":0}");
        n = ebo_rtm_shoot_mode(j, sizeof j, &c, 2);
        expect_str("rtm_shoot", j, n,
            "{\"id\":102035,\"sid\":\"fLaBO4gua8WF7v2u\",\"data\":{\"shootMode\":2},\"type\":0,\"timestamp\":0}");
        n = ebo_rtm_dock(j, sizeof j, &c);
        expect_str("rtm_dock", j, n,
            "{\"id\":103043,\"sid\":\"fLaBO4gua8WF7v2u\",\"type\":0,\"timestamp\":0}");
        {
            int emoji[1] = { 8 };
            n = ebo_rtm_emote(j, sizeof j, &c, emoji, 1, 0, 0, 0, 0, 0);
            expect_str("rtm_emote", j, n,
                "{\"id\":103003,\"sid\":\"fLaBO4gua8WF7v2u\",\"data\":{\"voiceIds\":[],\"cycleMode\":0,"
                "\"emojiIds\":[8],\"moveIds\":[]},\"type\":0,\"timestamp\":0}");
        }
    }

    printf("\n-- RTM parse (captured samples) --\n");
    {
        ebo_rtm_msg_t m;
        ebo_rtm_parse("{\"id\":103051,\"sid\":\"x\",\"data\":{\"laser\":true},\"type\":0}", &m);
        expect_int("parse_id", m.id, 103051);
        expect_int("parse_laser", m.has_laser && m.laser, 1);
        ebo_rtm_parse("{\"id\":101047,\"data\":{\"isSleeping\":false}}", &m);
        expect_int("parse_sleep", m.has_sleeping && !m.sleeping, 1);
        ebo_rtm_parse("{\"id\":102035,\"data\":{\"shootMode\":2}}", &m);
        expect_int("parse_shoot", m.has_shoot_mode && m.shoot_mode == 2, 1);
        ebo_rtm_parse("{\"id\":101005,\"data\":{\"state\":0}}", &m);
        expect_int("parse_state", m.has_state && m.state == 0, 1);
    }

    printf("\n-- inbound MAVLink status parse --\n");
    {
        /* BATTERY_STATUS (msgid 207): build a frame with remaining@byte22, charge@byte23. */
        ebo_status_t st;
        uint8_t f[64];
        int plen = 30, i, total;
        memset(f, 0, sizeof f);
        f[0] = 0xFE; f[1] = (uint8_t)plen; f[5] = 207;
        f[22] = 88;   /* battery_remaining */
        f[23] = 1;    /* charge_state */
        total = 6 + plen + 2;
        i = ebo_mav_parse_status(f, (size_t)total, &st);
        expect_int("battery_msgs", i, 1);
        expect_int("battery_pct", st.has_battery ? st.battery_remaining : -1, 88);
        expect_int("charge_state", st.has_battery ? st.charge_state : -1, 1);
    }

    printf("\n-- one-shot ebo_build routing --\n");
    {
        ebo_msg_t m;
        ebo_params_t p;
        memset(&p, 0, sizeof p);
        p.forward = 1.0f;
        /* SE drives over MAVLink */
        n = ebo_build(EBO_VARIANT_SE, EBO_ACT_DRIVE, &p, b, sizeof b, &m);
        expect_int("se_drive_ch", m.channel, EBO_CH_RDT_MAVLINK);
        expect_hex("se_drive", b, n, "fe11000000ca00000000000080bf0000000000000000003dfc");
        /* Air2 drives over RTM */
        p.rtm.sid = "S"; p.rtm.timestamp_ms = 0;
        n = ebo_build(EBO_VARIANT_AIR2, EBO_ACT_DRIVE, &p, b, sizeof b, &m);
        expect_int("air2_drive_ch", m.channel, EBO_CH_RTM_JSON);
        expect_str("air2_drive", (char *)b, n,
            "{\"id\":101007,\"sid\":\"S\",\"data\":{\"lx\":0,\"ly\":100,\"rx\":0,\"ry\":0,\"buttons\":0},"
            "\"type\":0,\"timestamp\":0}");
        /* Air2 laser over RTM; SE laser unsupported */
        p.on = 1;
        n = ebo_build(EBO_VARIANT_AIR2, EBO_ACT_LASER, &p, b, sizeof b, &m);
        expect_int("air2_laser_ch", m.channel, EBO_CH_RTM_JSON);
        n = ebo_build(EBO_VARIANT_SE, EBO_ACT_LASER, &p, b, sizeof b, &m);
        expect_int("se_laser_unsup", n, EBO_ERR_UNSUPPORTED);
        /* Air2 night vision is OFF-rtm -> routed to MAVLink */
        expect_int("air2_night_ch", ebo_route(EBO_VARIANT_AIR2, EBO_ACT_NIGHT), EBO_CH_RDT_MAVLINK);
    }

    printf("\n%s\n", g_fail ? "SOME TESTS FAILED" : "ALL TESTS PASSED");
    return g_fail ? 1 : 0;
}
