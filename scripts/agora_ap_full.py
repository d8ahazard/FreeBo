"""Dump the FULL RTC AP response (outer + every response_body buffer) to map ap_response assembly (ticket etc.)."""
import json, secrets, time
import httpx

s = httpx.get("http://localhost:8200/api/air2/session", timeout=15).json()
req = {"appid": s["app_id"], "client_ts": int(time.time()*1000), "opid": secrets.randbelow(10**12),
       "sid": secrets.token_hex(16).upper(),
       "request_bodies": [{"uri": 22, "buffer": {"cname": s["rtc"]["channel"],
                          "detail": {"11": "CN,GLOBAL", "17": "1", "22": "CN,GLOBAL"},
                          "key": s["rtc"]["token"], "service_ids": [11, 26], "uid": int(s["rtc"]["uid"])}}]}
r = httpx.post("https://webrtc2-ap-web-1.agora.io/api/v2/transpond/webrtc?v=2",
               files={"request": (None, json.dumps(req))}, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).json()
print("OUTER keys:", [k for k in r if k != "response_body"])
print("  enter_ts:", r.get("enter_ts"), "opid:", r.get("opid"), "detail:", r.get("detail"))
for i, e in enumerate(r.get("response_body", [])):
    buf = e.get("buffer", {})
    print(f"\nresponse_body[{i}] uri={e.get('uri')} buffer keys: {list(buf.keys())}")
    print("  detail keys:", list(buf.get("detail", {}).keys()))
    print("  has ticket:", "ticket" in buf, " has cert:", "cert" in buf)
    if "ticket" in buf:
        print("  TICKET:", str(buf['ticket'])[:60])
    print("  edges:", buf.get("edges_services"))
