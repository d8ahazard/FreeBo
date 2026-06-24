"""AgoraNativeReceiver — pulls the EBO Air 2's live A/V into Python server-side, natively, no browser/WSL.

This is the productionized form of the reverse-engineered media path (see docs/AIR2_CLOUD.md):

    AP lookup (HTTPS)  ->  edge gateway WS (AgoraWS)  ->  join_v3 (we are ICE-CONTROLLING)
      ->  collect on_add_*_stream announcements (video AND audio)
      ->  aiortc ICE + DTLS bring-up to the gateway media plane (standard DTLS-SRTP — verified)
      ->  subscribe each stream  ->  tap decrypted RTP off the DTLS transport
      ->  route by SSRC:  H265 (HEVC) -> our RFC7798 depacketizer -> PyAV decode -> BGR frame
                          Opus        -> PyAV decode -> 16 kHz mono PCM
      ->  publish to a MediaHub, which fans out to UI / brain / VSLAM / STT.

Everything heavy (aiortc, av, numpy) is imported lazily so this module is safe to import anywhere.

VSLAM note: every decoded frame carries the RTP 90 kHz timestamp and a keyframe flag, so a visual-inertial
SLAM consumer can fuse frames with the Air 2's 6-axis IMU telemetry and pick IRAP frames as relocalization
anchors. We never downsample at the source — consumers decide their own rate.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from collections import deque
from typing import Any, Optional

import httpx

from .agora_ws import AgoraWS
from .h265_rtp import H265Depacketizer
from .media_hub import AudioChunk, MediaHub, VideoFrame

AP_URL = "https://webrtc2-ap-web-1.agora.io/api/v2/transpond/webrtc?v=2"

# HEVC NAL unit types that are IRAP (instantaneous-decoder-refresh / clean random access) — i.e. keyframes a
# SLAM relocalizer can anchor on. BLA/IDR/CRA span 16..23.
_HEVC_IRAP = set(range(16, 24))

# G.711 A-law (PCMA, pt 8) — the channel audio codec the gateway advertises in sendrecv.audioCodecs. We
# declare it as a SEND codec so the gateway will forward the audio RTP we publish (talkback). Mirrors the
# server's own entry for PCMA so the negotiation matches exactly.
_PCMA_CODEC = {"payloadType": 8, "rtpMap": {"clockRate": 8000, "encodingName": "PCMA"},
               "rtcpFeedbacks": [{"type": "rrtr"}]}
_OPUS_CODEC = {"payloadType": 111,
               "rtpMap": {"clockRate": 48000, "encodingName": "opus", "encodingParameters": 2},
               "rtcpFeedbacks": [{"type": "transport-cc"}, {"type": "rrtr"}, {"type": "nack"}],
               "fmtp": {"parameters": {"minptime": "10", "useinbandfec": "1"}}}
_AUDIO_EXTS = [
    {"entry": 14, "extensionName": "urn:ietf:params:rtp-hdrext:ssrc-audio-level"},
    {"entry": 2, "extensionName": "http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time"},
    {"entry": 4, "extensionName": "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01"},
]


def build_rtp_capabilities(send_audio: bool = True) -> dict:
    """rtpCapabilities for the RTC join. Starts from the browser's proven caps (so video reception is
    unchanged) and ALWAYS declares RECV audio codecs + the audio header extensions the gateway lists.

    Why recv audio is mandatory: sniffing the EBO app showed its mic RTM handshake (102001/102003) is
    byte-identical to ours, yet with `rtpCapabilities=None` the robot announces `audio=None` and never streams
    its mic. The missing piece was telling the gateway we can RECEIVE audio — once declared, the robot
    publishes its mic and we can run STT/voice commands. `send_audio` additionally declares a PCMA SEND codec
    for talkback (robot speaker); without it the gateway silently drops anything we publish."""
    import pathlib
    caps = json.loads((pathlib.Path(__file__).with_name("agora_rtp_caps.json")).read_text(encoding="utf-8"))
    caps.setdefault("send", {}).setdefault("audioCodecs", [])
    caps.setdefault("recv", {}).setdefault("audioCodecs", [])
    # RECV audio — always on, so we hear the robot's mic (the fix for "it won't listen").
    caps["recv"]["audioCodecs"] = [_PCMA_CODEC, _OPUS_CODEC]
    caps["recv"]["audioExtensions"] = list(_AUDIO_EXTS)
    # SEND audio — only when talkback is enabled (publishing TTS onto the robot's speaker).
    if send_audio:
        caps["send"]["audioCodecs"] = [_PCMA_CODEC]
        caps["send"]["audioExtensions"] = []   # our published RTP carries no header extensions
    else:
        caps["send"]["audioCodecs"] = []
    return caps


async def ap_lookup(app_id: str, channel: str, uid: int, token: str) -> tuple[dict, list]:
    """Resolve the Agora edge gateway for this channel (the access-point step the web SDK does first)."""
    req = {"appid": app_id, "client_ts": int(time.time() * 1000), "opid": secrets.randbelow(10**12),
           "sid": secrets.token_hex(16).upper(),
           "request_bodies": [{"uri": 22, "buffer": {"cname": channel,
                               "detail": {"11": "CN,GLOBAL", "17": "1", "22": "CN,GLOBAL"},
                               "key": token, "service_ids": [11, 26], "uid": uid}}]}
    async with httpx.AsyncClient(timeout=20) as c:
        r = (await c.post(AP_URL, files={"request": (None, json.dumps(req))},
                          headers={"User-Agent": "Mozilla/5.0"})).json()
    bufs = [e["buffer"] for e in r["response_body"]]
    buf = next((b for b in bufs if "19" in b.get("detail", {})), bufs[0])
    detail = dict(buf.get("detail", {}))
    if r.get("detail", {}).get("502"):
        detail["502"] = r["detail"]["502"]
    ap = {"code": 0, "server_ts": r.get("enter_ts"), "uid": buf["uid"], "cid": buf["cid"], "cname": buf["cname"],
          "detail": detail, "flag": buf.get("flag", 0), "opid": r.get("opid"), "cert": buf["cert"],
          "ticket": buf["cert"]}
    return ap, buf["edges_services"]


class AgoraNativeReceiver:
    """Owns one native subscription to the robot's Agora channel and publishes decoded media to a MediaHub."""

    def __init__(self, session: Optional[dict] = None, hub: Optional[MediaHub] = None, *,
                 rtp_capabilities: Optional[dict] = None, session_provider=None) -> None:
        self.session = session
        self.session_provider = session_provider  # async () -> session dict (refreshed each reconnect)
        self.hub = hub or MediaHub()
        self._rtp_caps = rtp_capabilities
        self._running = False
        self._paused = False     # go-dark: keep the RTC/RTM session warm but drop media to the hub
        self._task: Optional[asyncio.Task] = None
        self.connected = False
        self.error: Optional[str] = None
        # SSRC -> ("video"|"audio", payload_type). Filled from on_add_*_stream announcements.
        self._streams: dict[int, tuple[str, int]] = {}
        # per-video-SSRC depacketizer + access-unit accumulator
        self._depkt: dict[int, H265Depacketizer] = {}
        self._au: dict[int, bytearray] = {}
        self._au_kf: dict[int, bool] = {}
        # The robot can announce more than one video SSRC (main + a sub/simulcast encoding). We decode and
        # display ONLY the first video stream; feeding two encodings' access units into one decoder context
        # desyncs its references and corrupts every other frame (white blocks / purple chroma). Belt-and-braces,
        # we also key the decoder by SSRC so streams can never share a context.
        self._primary_video_ssrc: Optional[int] = None
        self._vdec: dict[int, Any] = {}   # per-SSRC PyAV HEVC decoder (lazy)
        self._adec = None       # PyAV Opus decoder (lazy)
        self._aresampler = None
        self._seq = 0
        # robustness state
        self._last_seq: dict[int, int] = {}   # ssrc -> last RTP seq (gap = packet loss -> drop the AU)
        self._last_aseq: dict[int, int] = {}  # ssrc -> last AUDIO RTP seq (gap -> insert silence so STT/VAD stay aligned)
        self._au_lost: dict[int, bool] = {}   # ssrc -> current AU saw a gap
        self._got_kf: dict[int, bool] = {}    # ssrc -> have we seen a keyframe yet (gate P-frames)
        self._last_frame_ts = 0.0             # monotonic of last decoded video frame (stall watchdog)
        # telemetry parsed off the gateway signaling WS (battery/motion/sensors); unknown types logged once
        self.telemetry: dict[str, Any] = {}
        self._seen_types: set[str] = set()
        # media-path diagnostics (audio-in debugging): packet counts + any RTP on SSRCs we didn't subscribe.
        self._video_pkts = 0
        self._audio_pkts = 0
        self._unknown_ssrcs: set[int] = set()
        # Air 2 mic is quiet over G.711, but 8x clipped loud speech; 4x + per-utterance normalize in AudioSink
        # is cleaner. Tune with AUTOBOT_MIC_GAIN.
        self._mic_gain = float(os.environ.get("AUTOBOT_MIC_GAIN", "4"))
        # --- outbound audio publish (EXPERIMENTAL talkback) ---
        # When a session is live, `_pub` holds the handles needed to send Opus RTP back into the channel so
        # the robot's own speaker plays FreeBo's voice. The SRTP send is real; the gateway publish/set_source
        # announcement is NOT yet reverse-engineered, so a successful send is "audio emitted", not a guarantee
        # the robot played it — see docs/NATIVE_AIR2.md. Off by default; the link enables it via a flag.
        self._pub: Optional[dict] = None
        self._pub_ssrc = secrets.randbits(32) | 1
        self._pub_seq = 0
        self._pub_ts = 0
        self._pub_task: Optional[asyncio.Task] = None
        self._want_publish = False                # keep the talkback track alive across RTC reconnects
        # Queued G.711 frames as (playback_id, frame). Tagging each frame with its clip id lets barge-in cancel
        # a specific clip mid-stream (drop its remaining frames -> silence) WITHOUT killing the publish task —
        # continuous silence must keep flowing to sustain the robot's mic/call. See cancel_playback().
        self._tts_payloads: "deque[tuple[int, bytes]]" = deque()
        self._playback_seq = 0                    # monotonic clip id
        self._playbacks: dict[int, dict] = {}     # id -> handle {id, generation, expected_s, start_ts, cancelled}
        self._active_playback_id: Optional[int] = None
        self._cancelled_playbacks: set[int] = set()
        self._talk_on = os.environ.get("AUTOBOT_AIR2_NATIVE_TALK", "1").strip().lower() in (
            "1", "true", "yes", "on")

    # ---- lifecycle ----
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.ensure_future(self._run_forever())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:  # noqa: BLE001
                pass

    async def _run_forever(self) -> None:
        """Reconnect with backoff. CRITICAL: each (re)join forces a fresh cloud login, so a session that comes
        up but never delivers media must NOT be retried every second — that hammers the cloud login endpoint
        into 429 (a self-inflicted storm) and spawns zombie sessions. So we only reset the backoff when a
        session actually decoded frames; a fruitless session grows the backoff instead."""
        backoff = 2.0
        while self._running:
            frames_before = self._seq
            try:
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
                self.connected = False
            healthy = self._seq > frames_before          # did this session actually produce video?
            backoff = 2.0 if healthy else min(backoff * 1.7, 20.0)
            await asyncio.sleep(backoff)

    async def _dead_tap(self, data, arrival_time_ms: int = 0):
        """Async no-op RTP sink. aiortc AWAITS the rtp-data handler, so when we retire a session we must
        replace its tap with a coroutine (a plain lambda returning None makes aiortc do `await None` and
        crashes the DTLS receive task — which previously killed all media). This lets a retired session's
        still-open socket drain harmlessly instead of feeding decoded frames into the next session's state."""
        return None

    def _reset_session_media(self) -> None:
        """Clear per-session stream/decoder state so a rejoin re-elects a fresh primary video SSRC instead of
        staying pinned to a dead one from the previous session."""
        self._streams.clear()
        self._depkt.clear()
        self._au.clear()
        self._au_kf.clear()
        self._au_lost.clear()
        self._got_kf.clear()
        self._last_seq.clear()
        self._vdec.clear()
        self._primary_video_ssrc = None

    # ---- one session ----
    async def _run_once(self) -> None:
        self._reset_session_media()
        from aiortc import RTCCertificate
        from aiortc.rtcdtlstransport import (RTCDtlsFingerprint, RTCDtlsParameters,
                                             RTCDtlsTransport)
        from aiortc.rtcicetransport import (RTCIceCandidate, RTCIceGatherer,
                                            RTCIceParameters, RTCIceTransport)

        if self.session_provider is not None:
            try:
                self.session = await self.session_provider(True)   # fresh token each (re)join
            except TypeError:
                self.session = await self.session_provider()
        s = self.session
        if not isinstance(s, dict) or not s.get("ok"):
            raise RuntimeError(f"no session: {s}")
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
                "rtpCapabilities": self._rtp_caps, "version": "2"}

        edge = next((e for e in edges if e["port"] not in (443, 3478)), edges[0])
        host = edge["ip"].replace(".", "-") + ".edge.agora.io"
        ws = await AgoraWS.connect(host, edge["port"])
        join = {"_id": secrets.token_hex(3), "_type": "join_v3", "_message": {
            "p2p_id": 1, "session_id": secrets.token_hex(16).upper(), "app_id": app_id, "channel_key": token,
            "channel_name": ch, "sdk_version": "4.24.4", "mode": "rtc", "codec": "vp8", "role": "host",
            "has_changed_gateway": False, "ap_response": ap, "extend": "", "details": {},
            "features": {"rejoin": True}, "join_ts": int(time.time() * 1000), "ortc": ortc, "uid": ap["uid"]}}
        await ws.send_text(json.dumps(join))

        gw_ortc = None
        # Collect the gateway answer + every stream announcement we see in the first window.
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                op, m = await asyncio.wait_for(ws.recv(), timeout=max(0.2, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            if op != 0x1:
                continue
            j = json.loads(m)
            if j.get("_result") == "success" and j.get("_message", {}).get("ortc"):
                gw_ortc = j["_message"]["ortc"]
            elif str(j.get("_type", "")).startswith("on_add_") and "_message" in j:
                self._note_stream(j["_message"])
                if gw_ortc and self._streams:
                    break

        if not gw_ortc or not self._streams:
            raise RuntimeError(f"no stream/ortc (streams={self._streams})")

        # ICE/DTLS bring-up. We sent the offer (join) so we are ICE-CONTROLLING — without this aioice never
        # completes connectivity against Agora's gateway.
        gip = gw_ortc["iceParameters"]
        for c in gip.get("candidates", []):
            await ice.addRemoteCandidate(RTCIceCandidate(component=1, foundation=str(c.get("foundation", "0")),
                ip=c["ip"], port=c["port"], priority=c.get("priority", 1),
                protocol=c.get("protocol", "udp"), type=c.get("type", "host")))
        await ice.addRemoteCandidate(None)
        try:
            ice._connection.ice_controlling = True
            ice._role_set = True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.wait_for(ice.start(RTCIceParameters(usernameFragment=gip["iceUfrag"],
                                                          password=gip["icePwd"])), timeout=15)
        gdp = gw_ortc["dtlsParameters"]
        fps = [RTCDtlsFingerprint(algorithm=f.get("algorithm", "sha-256"), value=f["fingerprint"])
               for f in gdp["fingerprints"]]
        await asyncio.wait_for(dtls.start(RTCDtlsParameters(fingerprints=fps, role=gdp.get("role", "auto"))),
                               timeout=15)

        # Tap decrypted RTP directly (aiortc lacks an HEVC decoder; we route ourselves).
        from aiortc.rtp import RtpPacket
        loop = asyncio.get_event_loop()

        async def tap(data: bytes, arrival_time_ms: int):
            try:
                p = RtpPacket.parse(data, dtls._rtp_header_extensions_map)
            except Exception:  # noqa: BLE001
                return
            try:
                self._on_rtp(p)
            except Exception:  # noqa: BLE001
                pass
        dtls._handle_rtp_data = tap

        # Subscribe to every announced stream. stream_id MUST be the publisher (robot) uid — the gateway
        # won't start media for stream_id=None.
        for ssrc, info in self._streams.items():
            await ws.send_text(json.dumps({"_id": secrets.token_hex(3), "_type": "subscribe", "_message": {
                "stream_id": info["uid"], "stream_type": info["kind"], "mode": "rtc", "codec": "vp8",
                "p2p_id": 1, "twcc": True, "rtx": True, "extend": "", "ssrcId": ssrc}}))
        self.connected = True
        self.error = None
        self._last_frame_ts = time.monotonic()
        # expose the live transport for the on-demand outbound-audio publisher (talkback). Streaming only
        # starts when publish_audio()/ensure_publishing() is called (after the app sends RTM 102003).
        self._pub = {"ws": ws, "dtls": dtls, "app_id": app_id, "channel": ch, "uid": ap["uid"]}
        self._pub_task = None

        # Hold the session open. Keep reading the WS to (a) pick up streams announced AFTER join (the robot
        # publishes audio a moment after video), (b) parse inbound telemetry, and (c) run the FRAME-STALL
        # WATCHDOG — if decoded frames stop (ICE consent failure etc.) we tear down so _run_forever rejoins,
        # instead of silently going blind until a manual sleep/wake.
        have_video = any(i["kind"] == "video" for i in self._streams.values())
        seq_at_start = self._seq   # frames decoded before this session — for the first-frame watchdog grace
        try:
            last_ping = time.time()
            while self._running and dtls.state == "connected":
                # Split recv from processing: only a CONNECTION error breaks the session — a single malformed
                # or unexpected frame must never drop it (a stray TypeError here used to kill the whole loop,
                # clearing _pub and stalling media). Per-message errors are logged and skipped.
                try:
                    op, m = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    op = m = None
                except Exception as e:  # noqa: BLE001 — connection-level: rejoin
                    self.error = f"ws recv: {type(e).__name__}: {e}"
                    break
                if op is not None:
                    try:
                        await self._handle_ws_frame(op, m, ws)
                    except Exception:  # noqa: BLE001 — bad frame: skip, keep the session alive
                        pass
                # frame-stall watchdog: rejoin if video died. Give the FIRST frame a long grace (media can take
                # several seconds to start after subscribe); only use the tight 6s stall window once frames
                # have actually been flowing. Prevents premature rejoin->relogin churn on a slow media start.
                if have_video and self._last_frame_ts:
                    idle = time.monotonic() - self._last_frame_ts
                    grace = 12.0 if self._seq > seq_at_start else 18.0
                    if idle > grace:
                        self.error = "video stalled — rejoining"
                        break
                if time.time() - last_ping > 5:
                    last_ping = time.time()
                    try:
                        await ws.send_text(json.dumps({"_id": secrets.token_hex(3), "_type": "ping",
                                                       "_message": {"p2p_id": 1, "ts": int(time.time() * 1000)}}))
                    except Exception:  # noqa: BLE001
                        break
        finally:
            self.connected = False
            self._pub = None
            if self._pub_task:
                self._pub_task.cancel()
                self._pub_task = None
            # Retire this session's RTP tap with an ASYNC no-op so its lingering socket can't feed frames into
            # the next session (overlap was making _pub/connected churn even while video looked healthy).
            try:
                dtls._handle_rtp_data = self._dead_tap
            except Exception:  # noqa: BLE001
                pass
            # Send an explicit `leave` so the gateway removes our uid from the channel. Without it, every
            # torn-down session lingers as a ZOMBIE holding the robot's single viewer slot — the robot keeps
            # streaming to the dead session (its eye stays "online") and our NEXT session gets signaling but
            # no media. This was the real cause of the intermittent 0-frame joins.
            try:
                await ws.send_text(json.dumps({"_id": secrets.token_hex(3), "_type": "leave"}))
            except Exception:  # noqa: BLE001
                pass
            await ws.close()

    async def _handle_ws_frame(self, op: int, m: bytes, ws) -> None:
        """Process one signaling frame: gateway replies, late stream announcements (subscribe to them), and
        telemetry. Runs under a per-message try in the hold-open loop so a bad frame can't drop the session."""
        if op == 0x2:
            self._parse_ws_binary(m)
            return
        if op != 0x1:
            return
        j = json.loads(m)
        t = str(j.get("_type", ""))
        if t.startswith("on_add_") and "_message" in j:
            msg = j["_message"]
            if msg.get("ssrcId") is not None and msg.get("ssrcId") not in self._streams:
                self._note_stream(msg)
                info = self._streams.get(msg["ssrcId"])
                if info:
                    await ws.send_text(json.dumps({
                        "_id": secrets.token_hex(3), "_type": "subscribe", "_message": {
                            "stream_id": info["uid"], "stream_type": info["kind"], "mode": "rtc",
                            "codec": "vp8", "p2p_id": 1, "twcc": True, "rtx": True,
                            "extend": "", "ssrcId": msg["ssrcId"]}}))
        else:
            self._parse_ws_telemetry(j)

    # ---- telemetry off the signaling WS ----
    def _ingest_telemetry(self, d: dict) -> None:
        """Pull battery/charge/IMU/TOF/touch out of a robot status dict (shapes vary; mirror the browser parse)."""
        if not isinstance(d, dict):
            return
        inner = d.get("data") if isinstance(d.get("data"), dict) else d
        out: dict[str, Any] = {}
        pct = inner.get("percentage", inner.get("battery", inner.get("level", inner.get("electric"))))
        if isinstance(pct, (int, float)) and 0 <= pct <= 100:
            out["battery"] = int(pct)
        chg = inner.get("chargeStatus", inner.get("adapterStatus", inner.get("charging", inner.get("charge"))))
        if chg is not None:
            out["charge"] = 1 if (chg is True or (isinstance(chg, (int, float)) and chg > 0)) else 0
        for k in ("imu", "accel", "gyro", "tof", "distance", "obstacle", "wifi", "wifiStrength",
                  "touch", "touched", "bump", "speed", "moving", "velocity"):
            if k in inner:
                out[k] = inner[k]
        if out:
            self.telemetry.update(out)

    def _parse_ws_telemetry(self, j: dict) -> None:
        t = str(j.get("_type", "") or j.get("id", ""))
        if t and t not in self._seen_types:
            self._seen_types.add(t)
            print(f"[agora-native] ws msg type={t} keys={list(j.keys())[:8]}", flush=True)
        self._ingest_telemetry(j)
        if isinstance(j.get("_message"), dict):
            self._ingest_telemetry(j["_message"])

    def _parse_ws_binary(self, payload: bytes) -> None:
        """Robot data-stream telemetry can arrive as binary (sometimes zlib-compressed JSON). Best-effort."""
        for raw in (payload,):
            for candidate in (raw, self._maybe_inflate(raw)):
                if not candidate:
                    continue
                try:
                    j = json.loads(candidate)
                    self._parse_ws_telemetry(j if isinstance(j, dict) else {"data": j})
                    return
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _maybe_inflate(b: bytes) -> Optional[bytes]:
        import zlib
        for wbits in (15, -15, 47):
            try:
                return zlib.decompress(b, wbits)
            except Exception:  # noqa: BLE001
                continue
        return None

    # ---- stream bookkeeping ----
    def media_debug(self) -> dict:
        """Snapshot of the media path for diagnosing audio-in (exposed via /api/debug/rtm)."""
        return {"streams": {str(k): v for k, v in self._streams.items()},
                "video_pkts": self._video_pkts, "audio_pkts": self._audio_pkts,
                "unknown_ssrcs": [str(s) for s in self._unknown_ssrcs],
                "hub": self.hub.stats()}

    def _note_stream(self, msg: dict) -> None:
        ssrc = msg.get("ssrcId")
        if ssrc is None:
            return
        kind = "video" if msg.get("video") else "audio"
        self._streams[ssrc] = {"kind": kind, "pt": msg.get("pt", 0), "uid": msg.get("uid")}
        print(f"[agora-native] stream announced: kind={kind} ssrc={ssrc} uid={msg.get('uid')} "
              f"pt={msg.get('pt')} video={msg.get('video')} audio={msg.get('audio')} "
              f"keys={list(msg.keys())}", flush=True)
        if kind == "video":
            self._depkt[ssrc] = H265Depacketizer()
            self._au[ssrc] = bytearray()
            self._au_kf[ssrc] = False
            self._au_lost[ssrc] = False
            self._got_kf[ssrc] = False
            self._last_seq[ssrc] = -1
            if self._primary_video_ssrc is None:
                self._primary_video_ssrc = ssrc
                print(f"[agora-native] primary video ssrc={ssrc}", flush=True)
            elif ssrc != self._primary_video_ssrc:
                print(f"[agora-native] IGNORING secondary video ssrc={ssrc} "
                      f"(primary={self._primary_video_ssrc})", flush=True)

    def set_paused(self, on: bool) -> None:
        """Go-dark: when paused, drop all inbound media (video+audio) instead of publishing it to the hub, so
        SLAM/STT/MJPEG/the brain perceiver receive nothing — but the RTC session stays joined for instant wake."""
        self._paused = bool(on)

    # ---- RTP routing / decode ----
    def _on_rtp(self, p) -> None:
        if self._paused:
            return
        info = self._streams.get(p.ssrc)
        if info is None:
            # RTP on an SSRC we never subscribed/announced — log once (could be audio we're missing).
            if p.ssrc not in self._unknown_ssrcs:
                self._unknown_ssrcs.add(p.ssrc)
                print(f"[agora-native] RTP on UNKNOWN ssrc={p.ssrc} pt={p.payload_type} "
                      f"(not in announced streams)", flush=True)
            return
        if info["kind"] == "video":
            self._video_pkts += 1
            self._on_video_rtp(p)
        else:
            self._audio_pkts += 1
            if self._audio_pkts == 1:
                print(f"[agora-native] first AUDIO rtp ssrc={p.ssrc} pt={p.payload_type}", flush=True)
            # Codec: prefer the STREAM ANNOUNCEMENT's pt (the Air 2 announces pt=8 = G.711 A-law) — its RTP
            # header pt can lie (we saw 0). 0=µ-law, 8=A-law.
            pt = info.get("pt") if info.get("pt") in (0, 8, 9, 111) else p.payload_type
            self._on_audio_rtp(p, pt)

    def _on_video_rtp(self, p) -> None:
        # Only the primary video stream reaches the decoder/UI. A second announced encoding interleaved into
        # the pipeline is what produced the alternating white-block / purple-tint frames.
        if self._primary_video_ssrc is not None and p.ssrc != self._primary_video_ssrc:
            return
        depkt = self._depkt.get(p.ssrc)
        if depkt is None:
            return
        # Packet-loss detection: a gap in RTP sequence numbers means the current access unit is missing data,
        # so decoding it yields a white/distorted frame. Mark the AU lost and drop it.
        seq = p.sequence_number
        last = self._last_seq.get(p.ssrc, -1)
        if last >= 0 and seq != ((last + 1) & 0xFFFF):
            self._au_lost[p.ssrc] = True
        self._last_seq[p.ssrc] = seq

        au = self._au[p.ssrc]
        for nal in depkt.feed(bytes(p.payload)):
            au += nal
            if len(nal) >= 5 and ((nal[4] >> 1) & 0x3F) in _HEVC_IRAP:
                self._au_kf[p.ssrc] = True
        if p.marker:
            annexb = bytes(au)
            keyframe = self._au_kf[p.ssrc]
            lost = self._au_lost[p.ssrc]
            self._au[p.ssrc] = bytearray()
            self._au_kf[p.ssrc] = False
            self._au_lost[p.ssrc] = False
            if not annexb:
                return
            if lost:
                return                       # corrupt AU — skip (prevents white/garbage frames)
            if keyframe:
                self._got_kf[p.ssrc] = True
            if not self._got_kf.get(p.ssrc):
                return                       # no I-frame yet — P-frames would decode to garbage
            self._decode_video(p.ssrc, annexb, p.timestamp, keyframe)

    def _decode_video(self, ssrc: int, annexb: bytes, rtp_ts: int, keyframe: bool) -> None:
        try:
            import av
        except Exception:  # noqa: BLE001
            return
        vdec = self._vdec.get(ssrc)
        if vdec is None:
            vdec = av.codec.CodecContext.create("hevc", "r")
            self._vdec[ssrc] = vdec
        try:
            for pkt in vdec.parse(annexb):
                for fr in vdec.decode(pkt):
                    try:
                        bgr = fr.to_ndarray(format="bgr24")
                    except Exception as e:  # noqa: BLE001
                        bgr = None
                        self.error = f"to_ndarray: {type(e).__name__}: {e}"
                    if bgr is None:
                        continue
                    self._seq += 1
                    self._last_frame_ts = time.monotonic()   # feed the stall watchdog
                    self.hub.publish_video(VideoFrame(
                        bgr=bgr, width=fr.width, height=fr.height, seq=self._seq,
                        rtp_ts=rtp_ts, wall_ts=time.monotonic(), keyframe=keyframe, annexb=annexb))
        except Exception:  # noqa: BLE001
            pass

    def _on_audio_rtp(self, p, pt: int) -> None:
        """Decode the robot's mic RTP -> 16 kHz mono PCM for STT. The Air 2 streams G.711 (pt 8 = A-law,
        pt 0 = µ-law) for the mic — decode with audioop (no PyAV needed). Opus (pt 111) goes through PyAV."""
        payload = bytes(p.payload)
        if not payload:
            return
        if pt in (0, 8):
            try:
                import audioop
                pcm8 = audioop.alaw2lin(payload, 2) if pt == 8 else audioop.ulaw2lin(payload, 2)
                pcm16, self._arate_state = audioop.ratecv(pcm8, 2, 1, 8000, 16000,
                                                          getattr(self, "_arate_state", None))
                # The Air 2 mic is very quiet over G.711 (~150-700 RMS); apply a fixed saturating gain so the
                # downstream energy-VAD + STT (and UI playback) get a usable level. Tunable via AUTOBOT_MIC_GAIN.
                if self._mic_gain != 1.0:
                    pcm16 = audioop.mul(pcm16, 2, self._mic_gain)
                # Packet-loss concealment: G.711 has no jitter buffer here, so a gap in RTP sequence = a hole
                # in time. Insert silence for the missing packets so the VAD/STT cadence stays aligned (lost
                # speech becomes a clean pause instead of two unrelated words smashed together).
                seq = p.sequence_number
                last = self._last_aseq.get(p.ssrc, -1)
                if last >= 0:
                    gap = (seq - last - 1) & 0xFFFF
                    if 0 < gap <= 10 and pcm16:
                        silence = b"\x00" * len(pcm16)
                        for _ in range(gap):
                            self.hub.publish_audio(AudioChunk(pcm=silence, sample_rate=16000,
                                                              rtp_ts=p.timestamp, wall_ts=time.monotonic()))
                self._last_aseq[p.ssrc] = seq
                self.hub.publish_audio(AudioChunk(pcm=pcm16, sample_rate=16000,
                                                  rtp_ts=p.timestamp, wall_ts=time.monotonic()))
            except Exception:  # noqa: BLE001
                pass
            return
        # Opus (or anything PyAV can decode)
        try:
            import av
            import numpy as np  # noqa: F401
        except Exception:  # noqa: BLE001
            return
        if self._adec is None:
            self._adec = av.codec.CodecContext.create("opus", "r")
            self._aresampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        try:
            pkt = av.packet.Packet(payload)
            for fr in self._adec.decode(pkt):
                for rs in self._aresampler.resample(fr):
                    pcm = rs.to_ndarray().astype("<i2").tobytes()
                    self.hub.publish_audio(AudioChunk(pcm=pcm, sample_rate=16000,
                                                      rtp_ts=p.timestamp, wall_ts=time.monotonic()))
        except Exception:  # noqa: BLE001
            pass

    # ---- outbound audio publish (EXPERIMENTAL talkback) ----
    def can_publish(self) -> bool:
        """True when a live RTC session exists to send audio into."""
        return bool(self._pub) and self.connected

    @staticmethod
    def _encode_opus(wav_bytes: bytes) -> list[bytes]:
        """WAV bytes -> a list of Opus packet payloads (48 kHz mono, 20 ms frames). Pure encode; runs in a
        worker thread. Raises on failure (the caller reports it)."""
        import io

        import av
        container = av.open(io.BytesIO(wav_bytes))
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        enc = av.CodecContext.create("libopus", "w")
        enc.sample_rate = 48000
        enc.format = "s16"
        enc.layout = "mono"
        payloads: list[bytes] = []
        for frame in container.decode(audio=0):
            for rs in resampler.resample(frame):
                rs.pts = None
                for pkt in enc.encode(rs):
                    payloads.append(bytes(pkt))
        for pkt in enc.encode(None):   # flush
            payloads.append(bytes(pkt))
        return payloads

    @staticmethod
    def _encode_g711_alaw(wav_bytes: bytes) -> list[bytes]:
        """WAV bytes -> 20 ms G.711 A-law frames (160 bytes @ 8 kHz) — the channel's audio codec."""
        import audioop
        import io
        import wave
        w = wave.open(io.BytesIO(wav_bytes), "rb")
        sr, ch = w.getframerate(), w.getnchannels()
        pcm = w.readframes(w.getnframes())
        if ch == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        if sr != 8000:
            pcm, _ = audioop.ratecv(pcm, 2, 1, sr, 8000, None)
        alaw = audioop.lin2alaw(pcm, 2)
        return [alaw[i:i + 160] for i in range(0, len(alaw), 160) if alaw[i:i + 160]]

    @staticmethod
    def _alaw_silence() -> bytes:
        import audioop
        return audioop.lin2alaw(b"\x00\x00" * 160, 2)   # 20 ms silence @ 8 kHz

    def ensure_publishing(self) -> bool:
        """Start the outbound G.711 audio track (silence keepalive + queued TTS) if not already running."""
        if not self._pub or not self.connected:
            return False
        if self._pub_task and not self._pub_task.done():
            return True
        self._pub_task = asyncio.ensure_future(self._publish_loop(self._pub["dtls"]))
        return True

    async def _send_publish_offer(self) -> None:
        """Tell the gateway we're publishing an audio stream on our SSRC. Without this `publish` offer the
        gateway has no sender mapping for our RTP and silently drops it (so the robot speaker stays silent).
        Reverse-engineered from agora-rtc-sdk-ng: PUBLISH carries {state:"offer", ortc:[{stream_type:"audio",
        attributes:{...}, ssrcs:[{ssrcId}]}]}. The PCMA codec itself is declared in the join's send caps."""
        pub = self._pub or {}
        ws = pub.get("ws")
        if ws is None:
            return
        msg = {"_id": secrets.token_hex(3), "_type": "publish", "_message": {
            "state": "offer", "p2p_id": 1, "mode": "rtc", "extend": "", "twcc": False, "rtx": False,
            "ortc": [{"stream_type": "audio",
                      "attributes": {"dtx": False, "hq": False, "lq": False, "stereo": False, "speech": False},
                      "ssrcs": [{"ssrcId": self._pub_ssrc}]}]}}
        try:
            await ws.send_text(json.dumps(msg))
            print(f"[agora-native] sent publish offer ssrc={self._pub_ssrc}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[agora-native] publish offer failed: {e}", flush=True)

    async def _publish_loop(self, dtls) -> None:
        """Stream G.711 A-law RTP (pt 8) on our SSRC: silence to hold the call open, real TTS when queued."""
        from aiortc.rtp import RtpPacket
        await self._send_publish_offer()
        await asyncio.sleep(0.2)   # let the gateway register the sender before the first RTP lands
        silence = self._alaw_silence()
        sent = 0
        while self.connected:
            payload = silence
            if self._tts_payloads:
                pid, frame = self._tts_payloads.popleft()
                # Cancelled clip: drop its frame, emit silence instead (keeps the call/mic alive).
                payload = silence if pid in self._cancelled_playbacks else frame
                if not self._tts_payloads or self._tts_payloads[0][0] != pid:
                    self._finish_playback(pid)   # this clip's frames are exhausted
            pkt = RtpPacket(payload_type=8, sequence_number=self._pub_seq & 0xFFFF,
                            timestamp=self._pub_ts & 0xFFFFFFFF, ssrc=self._pub_ssrc, marker=False)
            pkt.payload = payload
            try:
                await dtls._send_rtp(pkt.serialize())
            except Exception as e:  # noqa: BLE001
                print(f"[agora-native] publish loop send failed after {sent}: {e}", flush=True)
                break
            self._pub_seq = (self._pub_seq + 1) & 0xFFFF
            self._pub_ts = (self._pub_ts + 160) & 0xFFFFFFFF   # 20 ms @ 8 kHz
            sent += 1
            if sent == 50:
                print("[agora-native] audio publish active (G.711 A-law)", flush=True)
            await asyncio.sleep(0.02)

    def _finish_playback(self, pid: int) -> None:
        hb = self._playbacks.get(pid)
        if hb is not None:
            hb["done"] = True
        if self._active_playback_id == pid:
            self._active_playback_id = None

    async def publish_audio(self, wav_bytes: bytes) -> dict:
        """Queue a TTS clip (WAV) to stream into the RTC channel as G.711 A-law so the robot speaker plays it.
        Starts the publish track if needed. The robot also needs intercom enabled (RTM 102003), sent by the
        link. Returns a `playback_id` so the clip can be cancelled (barge-in). Fail-soft."""
        if not self._pub or not self.connected:
            return {"ok": False, "error": "RTC channel not connected"}
        try:
            frames = await asyncio.to_thread(self._encode_g711_alaw, wav_bytes)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"g711 encode failed: {type(e).__name__}: {e}"}
        if not frames:
            return {"ok": False, "error": "no audio frames to send"}
        self._playback_seq += 1
        pid = self._playback_seq
        self._playbacks[pid] = {"id": pid, "generation": self._playback_seq,
                                "expected_s": round(len(frames) * 0.02, 2), "start_ts": time.monotonic(),
                                "cancelled": False, "done": False}
        self._active_playback_id = pid
        self.ensure_publishing()
        self._tts_payloads.extend((pid, f) for f in frames)
        return {"ok": True, "available": True, "sent_frames": len(frames), "robot_confirmed": False,
                "playback_id": pid,
                "note": "TTS queued on the G.711 publish track (confirm the robot speaker plays it)"}

    def cancel_playback(self, playback_id: Optional[int] = None) -> dict:
        """Barge-in: stop a TTS clip mid-stream. Invalidates that clip's queued frames (the publish loop emits
        silence in their place), leaves the publish task + RTC/call running (silence sustains the mic), and is
        idempotent. `None` cancels whatever is currently playing. Safe to call from any thread (deque/set ops
        are atomic in CPython; no await)."""
        pid = playback_id if playback_id is not None else self._active_playback_id
        if pid is None:
            return {"ok": True, "note": "nothing playing"}
        self._cancelled_playbacks.add(pid)
        # Drop this clip's still-queued frames (others, if any, survive).
        self._tts_payloads = deque((p, f) for (p, f) in self._tts_payloads if p != pid)
        hb = self._playbacks.get(pid)
        if hb is not None:
            hb["cancelled"] = True
        if self._active_playback_id == pid:
            self._active_playback_id = None
        # Bound the cancelled-set so it can't grow unbounded over a long session.
        if len(self._cancelled_playbacks) > 64:
            self._cancelled_playbacks = set(sorted(self._cancelled_playbacks)[-32:])
        return {"ok": True, "playback_id": pid}
