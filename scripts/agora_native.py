"""Native (browser-free, Windows) Agora RTC client for the EBO Air 2 — server-side video + audio + telemetry.

Replicates the Agora Web SDK join we reverse-engineered from the live capture:
  AP/unilbs lookup (HTTPS) -> gateway addr + cert/ticket
  WSS :gateway  -> join_v3 (our aiortc ICE/DTLS/RTP params) -> on_add_video_stream -> subscribe
  UDP DTLS-SRTP (aiortc) -> H265 RTP -> depacketize (h265_rtp) -> PyAV decode -> frames to the model
  field-100 data channel (zlib JSON, id 101026) -> telemetry (battery %, charge, wifi, ...)

This is the legit fully-server-side path: no browser in the media loop, no WSL, native on Windows.

STATUS: scaffold wired to the captured protocol. The AP-lookup (gateway + cert/ticket) is the one piece that
needs the HTTPS capture (scripts/agora_analyze can extract it once captured). Marked AP_LOOKUP below.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import zlib

import aiohttp  # for AP lookup + ws (aiortc pulls in aiohttp)
from aiortc import RTCCertificate, RTCRtpReceiver
from aiortc.rtcdtlstransport import RTCDtlsParameters, RTCDtlsFingerprint, RTCDtlsTransport
from aiortc.rtcicetransport import RTCIceGatherer, RTCIceParameters, RTCIceTransport, RTCIceCandidate
from aiortc.rtcrtpparameters import (RTCRtpReceiveParameters, RTCRtpCodecParameters,
                                     RTCRtpDecodingParameters)

from autobot.robot.h265_rtp import H265Depacketizer

SDK_VERSION = "4.24.4"


def _id() -> str:
    return secrets.token_hex(3)


async def ap_lookup(session: dict) -> dict:
    """AP/unilbs gateway lookup. Returns {gateway_host, gateway_port, ap_response}. The ap_response (uid, cid,
    cert, ticket) is embedded into join_v3. Reconstructed from the captured HTTPS lookup — see
    data/captures/agora_signaling.jsonl (dir 'http'/'xhr' to ap-web-*.agora.io)."""
    # AP_LOOKUP: fill the request shape from the captured fetch/XHR to ap-web-*.agora.io.
    raise NotImplementedError("AP lookup pending HTTPS capture — connect the robot once with the new build")


class AgoraNativeClient:
    def __init__(self, session: dict, on_video=None, on_telemetry=None):
        self.session = session
        self.on_video = on_video            # callback(av.VideoFrame)
        self.on_telemetry = on_telemetry    # callback(dict)
        self.depack = H265Depacketizer()
        self._ws = None
        self._ice = None
        self._dtls = None
        self._receiver = None

    async def _build_local_ortc(self):
        """Generate our ICE + DTLS params via aiortc (ORTC mode) for the join_v3 'ortc' block."""
        gatherer = RTCIceGatherer()
        await gatherer.gather()
        ice_params = gatherer.getLocalParameters()
        ice_cands = gatherer.getLocalCandidates()
        cert = RTCCertificate.generateCertificate()
        ice = RTCIceTransport(gatherer)
        dtls = RTCDtlsTransport(ice, [cert])
        dtls_params = dtls.getLocalParameters()
        self._ice = ice
        self._dtls = dtls
        fp = dtls_params.fingerprints[0]
        return {
            "iceParameters": {"iceUfrag": ice_params.usernameFragment, "icePwd": ice_params.password},
            "dtlsParameters": {"fingerprints": [{"hashFunction": fp.algorithm, "fingerprint": fp.value}]},
            "rtpCapabilities": _recv_caps(),
        }, ice_cands

    async def connect(self):
        ap = await ap_lookup(self.session)
        ortc, _cands = await self._build_local_ortc()
        url = f"wss://{ap['gateway_host']}:{ap['gateway_port']}"
        sess = aiohttp.ClientSession()
        self._ws = await sess.ws_connect(url)
        join = {"_id": _id(), "_type": "join_v3", "_message": {
            "p2p_id": 1, "session_id": secrets.token_hex(16).upper(),
            "app_id": self.session["app_id"], "channel_key": self.session["rtc"]["token"],
            "channel_name": self.session["rtc"]["channel"], "sdk_version": SDK_VERSION,
            "mode": "rtc", "codec": "vp8", "role": "host", "ap_response": ap["ap_response"],
            "ortc": ortc, "join_ts": int(time.time() * 1000), "uid": self.session["rtc"]["uid"],
        }}
        await self._ws.send_str(json.dumps(join))
        await self._signal_loop()

    async def _signal_loop(self):
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._on_text(json.loads(msg.data))
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await self._on_binary(msg.data)

    async def _on_text(self, m: dict):
        t = m.get("_type")
        if t is None and m.get("_message", {}).get("ortc"):
            await self._apply_remote_ortc(m["_message"]["ortc"])
        elif t == "on_add_video_stream":
            mm = m["_message"]
            await self._subscribe(mm["uid"], mm["ssrcId"], mm.get("pt", 49))

    async def _apply_remote_ortc(self, ortc: dict):
        ip = ortc["iceParameters"]
        for c in ip.get("candidates", []):
            self._ice.addRemoteCandidate(RTCIceCandidate(
                component=1, foundation=c.get("foundation", "0"), ip=c["ip"], port=c["port"],
                priority=c.get("priority", 1), protocol=c.get("protocol", "udp"), type=c.get("type", "host")))
        await self._ice.start(RTCIceParameters(usernameFragment=ip["iceUfrag"], password=ip["icePwd"]))
        dp = ortc["dtlsParameters"]
        fps = [RTCDtlsFingerprint(algorithm=f.get("algorithm", "sha-256"), value=f["fingerprint"]) for f in dp["fingerprints"]]
        await self._dtls.start(RTCDtlsParameters(fingerprints=fps, role=dp.get("role", "server")))

    async def _subscribe(self, uid, ssrc, pt):
        await self._ws.send_str(json.dumps({"_id": _id(), "_type": "subscribe", "_message": {
            "stream_id": uid, "stream_type": "video", "mode": "rtc", "codec": "vp8",
            "p2p_id": 1, "twcc": True, "rtx": True, "extend": "", "ssrcId": ssrc}}))
        codec = RTCRtpCodecParameters(mimeType="video/H265", clockRate=90000, payloadType=pt)
        self._receiver = RTCRtpReceiver("video", self._dtls)
        self._receiver.receive(RTCRtpReceiveParameters(
            codecs=[codec], encodings=[RTCRtpDecodingParameters(ssrc=ssrc, payloadType=pt)]))
        asyncio.create_task(self._video_loop())

    async def _video_loop(self):
        import av
        decoder = av.CodecContext.create("hevc", "r")
        track = self._receiver.track
        while True:
            frame = await track.recv()  # aiortc gives us depacketized payloads/encoded frames
            if self.on_video:
                self.on_video(frame)

    async def _on_binary(self, data: bytes):
        # field-100 data channel carries zlib-JSON telemetry (battery etc.). Best-effort extract.
        try:
            payloads = _extract_zlib_json(data)
            for j in payloads:
                if self.on_telemetry:
                    self.on_telemetry(j)
        except Exception:  # noqa: BLE001
            pass


def _recv_caps() -> dict:
    return {"recv": {"audioCodecs": [], "videoCodecs": [
        {"payloadType": 49, "rtpMap": {"encodingName": "H265", "clockRate": 90000},
         "fmtp": {"parameters": {"profile-id": "1", "tier-flag": "0", "level-id": "123", "tx-mode": "SRST"}},
         "rtcpFeedbacks": [{"type": "nack"}, {"type": "nack", "parameter": "pli"}, {"type": "transport-cc"}]}],
        "videoExtensions": []}}


def _rv(b, i):
    s = 0; o = 0
    while i < len(b):
        c = b[i]; i += 1; o |= (c & 0x7F) << s
        if not c & 0x80:
            return o, i
        s += 7
    return o, i


def _walk(b):
    i = 0; out = []
    while i < len(b):
        tag, i = _rv(b, i); f = tag >> 3; w = tag & 7
        if w == 0:
            v, i = _rv(b, i); out.append((f, 0, v))
        elif w == 2:
            ln, i = _rv(b, i); out.append((f, 2, b[i:i + ln])); i += ln
        else:
            break
    return out


def _extract_zlib_json(raw: bytes) -> list[dict]:
    out = []
    top = _walk(raw)
    if not top or top[0][:2] != (1, 0) or top[0][2] != 100:
        return out
    sub = next((v for f, w, v in top if f == 2 and w == 2), None)
    for f, w, v in (_walk(sub) if sub else []):
        if w == 2:
            try:
                out.append(json.loads(zlib.decompress(v).decode("utf-8")))
            except Exception:  # noqa: BLE001
                pass
    return out
