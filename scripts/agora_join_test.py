"""Native Agora gateway JOIN test: AP lookup -> WSS gateway -> join_v3 (aiortc ICE/DTLS) -> print response.
Validates Agora accepts a browser-free join and returns the gateway's ICE/DTLS (the keys to the media stream)."""
import asyncio, json, secrets, time
import httpx
from aiortc import RTCCertificate
from aiortc.rtcdtlstransport import RTCDtlsTransport
from aiortc.rtcicetransport import RTCIceGatherer, RTCIceTransport
from autobot.robot.agora_ws import AgoraWS

AP_URL = "https://webrtc2-ap-web-1.agora.io/api/v2/transpond/webrtc?v=2"


async def ap_lookup(app_id, ch, uid, token):
    req = {"appid": app_id, "client_ts": int(time.time()*1000), "opid": secrets.randbelow(10**12),
           "sid": secrets.token_hex(16).upper(),
           "request_bodies": [{"uri": 22, "buffer": {"cname": ch, "detail": {"11": "CN,GLOBAL", "17": "1", "22": "CN,GLOBAL"},
                               "key": token, "service_ids": [11, 26], "uid": uid}}]}
    async with httpx.AsyncClient(timeout=20) as c:
        r = (await c.post(AP_URL, files={"request": (None, json.dumps(req))}, headers={"User-Agent": "Mozilla/5.0"})).json()
    # pick the buffer that has detail key '19' (peer DTLS fingerprints) + a non-443 gateway
    bufs = [e["buffer"] for e in r["response_body"]]
    buf = next((b for b in bufs if "19" in b.get("detail", {})), bufs[0])
    detail = dict(buf.get("detail", {}))
    if r.get("detail", {}).get("502"):
        detail["502"] = r["detail"]["502"]
    ap_response = {"code": 0, "server_ts": r.get("enter_ts"), "uid": buf["uid"], "cid": buf["cid"],
                   "cname": buf["cname"], "detail": detail, "flag": buf.get("flag", 0),
                   "opid": r.get("opid"), "cert": buf["cert"], "ticket": buf["cert"]}
    return ap_response, buf["edges_services"]


def _captured_rtp_caps():
    """Use the real, complete rtpCapabilities from the capture verbatim (the gateway parser wants the full set)."""
    import json, glob
    for fn in ["data/captures/agora_signaling.prev.jsonl", "data/captures/agora_signaling.prev2.jsonl"]:
        try:
            for l in open(fn, encoding="utf-8"):
                r = json.loads(l)
                if r.get("kind") == "text" and r.get("dir") == "send" and '"join_v3"' in r.get("data", ""):
                    return json.loads(r["data"])["_message"]["ortc"]["rtpCapabilities"]
        except Exception:
            pass
    return None


async def local_ortc():
    g = RTCIceGatherer(); await g.gather()
    p = g.getLocalParameters()
    cert = RTCCertificate.generateCertificate()
    ice = RTCIceTransport(g)
    dtls = RTCDtlsTransport(ice, [cert])
    fp = dtls.getLocalParameters().fingerprints[0]
    ortc = {"iceParameters": {"iceUfrag": p.usernameFragment, "icePwd": p.password},
            "dtlsParameters": {"fingerprints": [{"hashFunction": fp.algorithm, "fingerprint": fp.value}]},
            "rtpCapabilities": _captured_rtp_caps(), "version": "2"}
    return ice, dtls, ortc


async def main():
    s = httpx.get("http://localhost:8200/api/air2/session", timeout=15).json()
    app_id = s["app_id"]; ch = s["rtc"]["channel"]; uid = int(s["rtc"]["uid"]); token = s["rtc"]["token"]
    ap_response, edges = await ap_lookup(app_id, ch, uid, token)
    ice, dtls, ortc = await local_ortc()
    edge = next((e for e in edges if e["port"] not in (443, 3478)), edges[0])
    host = edge["ip"].replace(".", "-") + ".edge.agora.io"
    print("gateway:", f"wss://{host}:{edge['port']}", " uid:", ap_response["uid"])
    attributes = {"userAttributes": {"enableAudioMetadata": False, "enablePublishedUserList": True,
                  "enableUserList": False, "maxSubscription": 50, "enableUserLicenseCheck": True, "enableRTX": True,
                  "enableSubTWCC": True, "enablePubRTX": True, "enableSubRTX": True, "enableLossbasedBwe": True,
                  "enableAutCC": True, "enableAutFeedback": True, "enableUserAutoRebalanceCheck": True, "enableXR": True}}
    join = {"_id": secrets.token_hex(3), "_type": "join_v3", "_message": {
        "p2p_id": 1, "session_id": secrets.token_hex(16).upper(), "app_id": app_id,
        "channel_key": token, "channel_name": ch, "sdk_version": "4.24.4",
        "browser": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "process_id": "process-" + secrets.token_hex(8), "mode": "rtc", "codec": "vp8", "role": "host",
        "has_changed_gateway": False, "ap_response": ap_response,
        "extend": "", "details": {}, "features": {"rejoin": True}, "attributes": attributes,
        "join_ts": int(time.time()*1000), "ortc": ortc, "uid": ap_response["uid"]}}
    ws = await AgoraWS.connect(host, edge["port"])
    await ws.send_text(json.dumps(join))
    print("join_v3 sent; waiting for responses...")
    for _ in range(8):
        try:
            opcode, m = await asyncio.wait_for(ws.recv(), timeout=10)
            if opcode == 0x2:
                print("  [bin]", len(m), m[:24].hex()); continue
            j = json.loads(m)
            print("  [text]", j.get("_type") or "(no_type)", "result=", j.get("_result"), json.dumps(j)[:280])
            if j.get("_result") == "success" or "ortc" in j.get("_message", {}):
                print("\nNATIVE_JOIN_OK — Agora accepted our browser-free join!")
        except asyncio.TimeoutError:
            print("  (timeout)"); break
    await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
