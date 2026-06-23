"""Validate native reproduction of the Agora RTC AP/unilbs lookup (server-side, no browser).
Fetches our session, replays the captured FormData request, prints the gateway edges + cert it returns."""
import json, secrets, time, sys
import httpx

AP_URL = "https://webrtc2-ap-web-1.agora.io/api/v2/transpond/webrtc?v=2"


def main():
    # session from the running FreeBo server (signed Enabot REST + Agora tokens)
    s = httpx.get("http://localhost:8200/api/air2/session", timeout=15).json()
    if not s.get("ok"):
        print("session error:", s); return 1
    app_id = s["app_id"]; ch = s["rtc"]["channel"]; uid = int(s["rtc"]["uid"]); token = s["rtc"]["token"]
    print(f"session: app_id={app_id} channel={ch} uid={uid} token={token[:24]}...")

    req = {
        "appid": app_id, "client_ts": int(time.time() * 1000),
        "opid": secrets.randbelow(10**12), "sid": secrets.token_hex(16).upper(),
        "request_bodies": [{"uri": 22, "buffer": {
            "cname": ch, "detail": {"11": "CN,GLOBAL", "17": "1", "22": "CN,GLOBAL"},
            "key": token, "service_ids": [11, 26], "uid": uid}}],
    }
    # multipart/form-data, single field "request" = JSON string (matches the captured FormData)
    r = httpx.post(AP_URL, files={"request": (None, json.dumps(req))}, timeout=20,
                   headers={"User-Agent": "Mozilla/5.0", "Origin": "https://webrtc2-ap-web-1.agora.io"})
    print("HTTP", r.status_code)
    try:
        body = r.json()
    except Exception:
        print("non-json:", r.text[:500]); return 1
    rb = (body.get("response_body") or [])
    print("response_body entries:", len(rb))
    for e in rb[:3]:
        buf = e.get("buffer", {})
        print("  uid:", buf.get("uid"), "cid:", buf.get("cid"), "edges:", buf.get("edges_services"),
              "cert?", bool(buf.get("cert")))
    if rb and rb[0].get("buffer", {}).get("edges_services"):
        print("\nNATIVE_AP_OK — we reproduced the Agora gateway lookup server-side!")
        return 0
    print("\nNo edges returned — body:", json.dumps(body)[:600])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
