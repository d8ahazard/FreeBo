"""Native media bring-up test: join -> apply gateway ICE/DTLS (aiortc) -> subscribe -> receive RTP -> decode.
Reports each stage (ICE, DTLS, first RTP/frame) so we can iterate the hard media leg."""
import asyncio, json, secrets, time
import httpx
from aiortc import RTCCertificate, RTCRtpReceiver
from aiortc.rtcdtlstransport import RTCDtlsParameters, RTCDtlsFingerprint, RTCDtlsTransport
from aiortc.rtcicetransport import RTCIceGatherer, RTCIceParameters, RTCIceTransport, RTCIceCandidate
from aiortc.rtcrtpparameters import RTCRtpReceiveParameters, RTCRtpCodecParameters, RTCRtpDecodingParameters
from autobot.robot.agora_ws import AgoraWS

AP_URL = "https://webrtc2-ap-web-1.agora.io/api/v2/transpond/webrtc?v=2"


async def ap_lookup(app_id, ch, uid, token):
    req = {"appid": app_id, "client_ts": int(time.time()*1000), "opid": secrets.randbelow(10**12),
           "sid": secrets.token_hex(16).upper(),
           "request_bodies": [{"uri": 22, "buffer": {"cname": ch, "detail": {"11": "CN,GLOBAL", "17": "1", "22": "CN,GLOBAL"},
                               "key": token, "service_ids": [11, 26], "uid": uid}}]}
    async with httpx.AsyncClient(timeout=20) as c:
        r = (await c.post(AP_URL, files={"request": (None, json.dumps(req))}, headers={"User-Agent": "Mozilla/5.0"})).json()
    bufs = [e["buffer"] for e in r["response_body"]]
    buf = next((b for b in bufs if "19" in b.get("detail", {})), bufs[0])
    detail = dict(buf.get("detail", {}))
    if r.get("detail", {}).get("502"):
        detail["502"] = r["detail"]["502"]
    ap = {"code": 0, "server_ts": r.get("enter_ts"), "uid": buf["uid"], "cid": buf["cid"], "cname": buf["cname"],
          "detail": detail, "flag": buf.get("flag", 0), "opid": r.get("opid"), "cert": buf["cert"], "ticket": buf["cert"]}
    return ap, buf["edges_services"]


def caps():
    for fn in ["data/captures/agora_signaling.prev.jsonl", "data/captures/agora_signaling.prev2.jsonl"]:
        try:
            for l in open(fn, encoding="utf-8"):
                r = json.loads(l)
                if r.get("kind") == "text" and r.get("dir") == "send" and '"join_v3"' in r.get("data", ""):
                    return json.loads(r["data"])["_message"]["ortc"]["rtpCapabilities"]
        except Exception:
            pass


async def main():
    s = httpx.get("http://localhost:8200/api/air2/session", timeout=15).json()
    app_id = s["app_id"]; ch = s["rtc"]["channel"]; uid = int(s["rtc"]["uid"]); token = s["rtc"]["token"]
    ap, edges = await ap_lookup(app_id, ch, uid, token)

    gatherer = RTCIceGatherer(); await gatherer.gather()
    lp = gatherer.getLocalParameters()
    cert = RTCCertificate.generateCertificate()
    ice = RTCIceTransport(gatherer)
    dtls = RTCDtlsTransport(ice, [cert])
    fp = dtls.getLocalParameters().fingerprints[0]
    ortc = {"iceParameters": {"iceUfrag": lp.usernameFragment, "icePwd": lp.password},
            "dtlsParameters": {"fingerprints": [{"hashFunction": fp.algorithm, "fingerprint": fp.value}]},
            "rtpCapabilities": caps(), "version": "2"}

    edge = next((e for e in edges if e["port"] not in (443, 3478)), edges[0])
    host = edge["ip"].replace(".", "-") + ".edge.agora.io"
    ws = await AgoraWS.connect(host, edge["port"])
    join = {"_id": secrets.token_hex(3), "_type": "join_v3", "_message": {
        "p2p_id": 1, "session_id": secrets.token_hex(16).upper(), "app_id": app_id, "channel_key": token,
        "channel_name": ch, "sdk_version": "4.24.4", "mode": "rtc", "codec": "vp8", "role": "host",
        "has_changed_gateway": False, "ap_response": ap, "extend": "", "details": {},
        "features": {"rejoin": True}, "join_ts": int(time.time()*1000), "ortc": ortc, "uid": ap["uid"]}}
    await ws.send_text(json.dumps(join))

    gw_ortc = None
    robot = None
    for _ in range(8):
        try:
            op, m = await asyncio.wait_for(ws.recv(), timeout=10)
        except asyncio.TimeoutError:
            break
        if op != 0x1:
            continue
        j = json.loads(m)
        if j.get("_result") == "success" and j.get("_message", {}).get("ortc"):
            gw_ortc = j["_message"]["ortc"]
            print("JOIN ok; gateway ICE candidates:", gw_ortc["iceParameters"].get("candidates"))
        elif j.get("_type") == "on_add_video_stream":
            robot = j["_message"]
            print("robot stream:", robot)
            break

    if not gw_ortc or not robot:
        print("no stream/ortc; abort"); await ws.close(); return

    # --- ICE/DTLS bring-up to the gateway media plane ---
    gip = gw_ortc["iceParameters"]
    for c in gip.get("candidates", []):
        await ice.addRemoteCandidate(RTCIceCandidate(component=1, foundation=str(c.get("foundation", "0")),
            ip=c["ip"], port=c["port"], priority=c.get("priority", 1),
            protocol=c.get("protocol", "udp"), type=c.get("type", "host")))
    await ice.addRemoteCandidate(None)  # end-of-candidates
    try:
        ice._connection.ice_controlling = True   # we sent the offer (join) -> controlling
        ice._role_set = True
    except Exception as e:  # noqa: BLE001
        print("role set warn:", e)
    print("local ICE candidates:", len(gatherer.getLocalCandidates()))
    print("starting ICE ...")
    await asyncio.wait_for(ice.start(RTCIceParameters(usernameFragment=gip["iceUfrag"], password=gip["icePwd"])), timeout=15)
    print("ICE state:", ice.state)
    gdp = gw_ortc["dtlsParameters"]
    fps = [RTCDtlsFingerprint(algorithm=f.get("algorithm", "sha-256"), value=f["fingerprint"]) for f in gdp["fingerprints"]]
    print("starting DTLS (remote role=%s) ..." % gdp.get("role"))
    await asyncio.wait_for(dtls.start(RTCDtlsParameters(fingerprints=fps, role=gdp.get("role", "auto"))), timeout=15)
    print("DTLS state:", dtls.state)

    # --- tap raw decrypted RTP directly off the DTLS transport (aiortc has no H265 decoder) ---
    ssrc = robot["ssrcId"]; pt = robot["pt"]
    from aiortc.rtp import RtpPacket
    rtpq: asyncio.Queue = asyncio.Queue()
    stats = {"n": 0, "bytes": 0, "ssrcs": {}, "pts": {}}

    async def tap(data: bytes, arrival_time_ms: int):
        try:
            p = RtpPacket.parse(data, dtls._rtp_header_extensions_map)
        except Exception:
            return
        stats["n"] += 1; stats["bytes"] += len(p.payload)
        stats["ssrcs"][p.ssrc] = stats["ssrcs"].get(p.ssrc, 0) + 1
        stats["pts"][p.payload_type] = stats["pts"].get(p.payload_type, 0) + 1
        await rtpq.put(p)
    dtls._handle_rtp_data = tap  # bound override

    await ws.send_text(json.dumps({"_id": secrets.token_hex(3), "_type": "subscribe", "_message": {
        "stream_id": robot["uid"], "stream_type": "video", "mode": "rtc", "codec": "vp8",
        "p2p_id": 1, "twcc": True, "rtx": True, "extend": "", "ssrcId": ssrc}}))
    print("subscribed ssrc=%s pt=%s; tapping raw RTP ..." % (ssrc, pt))

    from autobot.robot.h265_rtp import H265Depacketizer
    import av
    depkt = H265Depacketizer()
    dec = av.codec.CodecContext.create("hevc", "r")
    au = bytearray(); frames = 0; t0 = time.time()
    while time.time() - t0 < 20 and frames < 5:
        try:
            p = await asyncio.wait_for(rtpq.get(), timeout=10)
        except asyncio.TimeoutError:
            print("  no RTP (timeout). stats:", stats); break
        for nal in depkt.feed(bytes(p.payload)):
            au += nal
        if p.marker and au:  # end of access unit -> decode
            try:
                for pkt in dec.parse(bytes(au)):
                    for fr in dec.decode(pkt):
                        frames += 1
                        print(f"FRAME {frames}: {fr.width}x{fr.height} pts={fr.pts}")
            except Exception as e:  # noqa: BLE001
                pass
            au = bytearray()
    print("done. rtp stats:", stats, "decoded frames:", frames)
    await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
