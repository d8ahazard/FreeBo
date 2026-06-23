"""Compare the captured join_v3 template vs what our native AP lookup returns, to match required fields."""
import json, glob, secrets, time
import httpx

tmpl = None
for fn in ["data/captures/agora_signaling.prev.jsonl", "data/captures/agora_signaling.prev2.jsonl",
           "data/captures/agora_signaling.jsonl"]:
    try:
        for l in open(fn, encoding="utf-8"):
            r = json.loads(l)
            if r.get("kind") == "text" and r.get("dir") == "send" and '"join_v3"' in r.get("data", ""):
                tmpl = json.loads(r["data"])["_message"]
                break
    except Exception:
        pass
    if tmpl:
        break

if tmpl:
    print("join_v3 _message keys:", list(tmpl.keys()))
    print("ap_response keys:", list(tmpl.get("ap_response", {}).keys()))
    print("ap_response.detail keys:", list(tmpl.get("ap_response", {}).get("detail", {}).keys()))
    print("has ticket in ap_response:", "ticket" in tmpl.get("ap_response", {}))
    print("attributes:", json.dumps(tmpl.get("attributes"))[:400])
    print("features:", json.dumps(tmpl.get("features")))
    print("ortc keys:", list(tmpl.get("ortc", {}).keys()))
else:
    print("no captured join_v3 template found")

# our AP buffer
s = httpx.get("http://localhost:8200/api/air2/session", timeout=15).json()
req = {"appid": s["app_id"], "client_ts": int(time.time()*1000), "opid": secrets.randbelow(10**12),
       "sid": secrets.token_hex(16).upper(),
       "request_bodies": [{"uri": 22, "buffer": {"cname": s["rtc"]["channel"],
                          "detail": {"11": "CN,GLOBAL", "17": "1", "22": "CN,GLOBAL"},
                          "key": s["rtc"]["token"], "service_ids": [11, 26], "uid": int(s["rtc"]["uid"])}}]}
r = httpx.post("https://webrtc2-ap-web-1.agora.io/api/v2/transpond/webrtc?v=2",
               files={"request": (None, json.dumps(req))}, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).json()
buf = r["response_body"][0]["buffer"]
print("\nAP buffer keys:", list(buf.keys()))
print("AP buffer.detail keys:", list(buf.get("detail", {}).keys()))
print("AP buffer has ticket:", "ticket" in buf)
