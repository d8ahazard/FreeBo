"""Autobot web server: REST + WebSocket + video proxy, and it serves the UI.

This is the single app the user opens. It owns the RobotLink (native or mock) and the AgentBrain, broadcasts
events (telemetry, AI thoughts, actions) to the browser over WebSocket, proxies video (WHEP/HLS) from the
local mediamtx so the UI is same-origin, and exposes manual controls + an always-available emergency stop.

Run:  python -m autobot
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..brain import notify, summarizer, tts
from ..brain.agent import AgentBrain
from ..brain.identity import Identity
from ..brain.memory import Memory
from ..brain.providers import catalog_for_ui, get_provider
from ..config import SETTINGS
from ..robot.frames import param_set_frame
from ..robot.link import make_link
from ..robot.overseer_gate import OverseerGate, ProposalStore
from . import onboarding as _onboarding

REPO_ROOT = Path(__file__).resolve().parents[2]
WEBUI_DIST = REPO_ROOT / "webui" / "dist"
WEBUI_SRC = REPO_ROOT / "webui" / "src"
FALLBACK_HTML = Path(__file__).parent / "static" / "fallback.html"


def _git_commit() -> str:
    """Short source commit from .git (no subprocess), for the build-provenance surface (P0-R4.8)."""
    try:
        head = (REPO_ROOT / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            return (REPO_ROOT / ".git" / ref).read_text(encoding="utf-8").strip()[:12]
        return head[:12]
    except Exception:  # noqa: BLE001
        return ""


_BUILD_HASH_CACHE: dict = {"mtime": None, "asset": None, "sha256": None}


def _frontend_build() -> dict:
    """Build provenance for the served production frontend (P0-R4.8): the asset filename + a content hash of
    the bytes we actually serve, the source commit, and a staleness flag (any webui/src file newer than the
    built bundle). The hash is cached by index.html mtime so /api/state stays cheap."""
    import hashlib
    import re
    info = {"dist_present": False, "asset": None, "asset_sha256": None,
            "source_commit": _git_commit(), "stale": None}
    idx = WEBUI_DIST / "index.html"
    if not idx.exists():
        return info
    info["dist_present"] = True
    try:
        mt = idx.stat().st_mtime
        if _BUILD_HASH_CACHE["mtime"] != mt:
            html = idx.read_text(encoding="utf-8")
            m = re.search(r"/assets/(index-[\w-]+\.js)", html)
            asset = m.group(1) if m else None
            sha = None
            if asset:
                p = WEBUI_DIST / "assets" / asset
                if p.exists():
                    sha = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            _BUILD_HASH_CACHE.update(mtime=mt, asset=asset, sha256=sha)
        info["asset"] = _BUILD_HASH_CACHE["asset"]
        info["asset_sha256"] = _BUILD_HASH_CACHE["sha256"]
        if WEBUI_SRC.exists():
            newest = max((f.stat().st_mtime for f in WEBUI_SRC.rglob("*") if f.is_file()), default=0.0)
            info["stale"] = newest > idx.stat().st_mtime
    except Exception:  # noqa: BLE001
        pass
    return info


app = FastAPI(title="FreeBo")

# --- the single robot link + brain (with memory + identity) for this process ---
LINK = make_link(SETTINGS.snapshot())
MEMORY = Memory()
IDENTITY = Identity(emit=lambda ev: emit(ev))
# Overseer puppet mode: the brain talks to the robot through a gate that, when `settings.overseer` is on,
# intercepts every robot-affecting call (records it as a proposal, returns a synthetic OK) so the dumb brain
# is paralyzed while a human/agent overseer drives the real LINK directly via /api/overseer/*. When overseer
# is off the gate is a transparent passthrough, so normal operation is unchanged. See overseer_gate.py.
PROPOSALS = ProposalStore()
BRAIN_LINK = OverseerGate(LINK, SETTINGS, PROPOSALS, lambda ev: emit(ev))
brain = AgentBrain(SETTINGS, lambda ev: emit(ev), BRAIN_LINK, MEMORY, IDENTITY)
ONBOARD = _onboarding.Onboarding(LINK, SETTINGS, IDENTITY, brain, lambda ev: emit(ev))

# --- the live media fan-out: the camera feed is decoded once and shared with the UI preview, the VSLAM
# mapper, and (via the brain) the AI. Frames arrive either from the native receiver or the browser bridge. ---
from ..robot.media_hub import MediaHub, VideoFrame  # noqa: E402
from ..brain.slam import VisualSlam  # noqa: E402

MEDIA_HUB = MediaHub()
SLAM = VisualSlam()
AUDIO_SINK = None
VISUAL_REFLEX = None
_frame_seq = 0


def _active_hub() -> MediaHub:
    """The hub the UI/VSLAM read from. Native links own their own hub (fed by the RTC receiver); other links
    use the shared hub fed by browser frame POSTs."""
    return getattr(LINK, "hub", None) or MEDIA_HUB


def _publish_frame(jpeg: bytes) -> None:
    """Decode an inbound JPEG camera frame and publish it to the media hub (UI preview + VSLAM read it).
    Best-effort + cheap (imdecode of a ~640px frame is ~1ms); never raises into the request path."""
    global _frame_seq
    try:
        import time as _t

        import cv2
        import numpy as np
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return
        _frame_seq += 1
        h, w = bgr.shape[:2]
        MEDIA_HUB.publish_video(VideoFrame(bgr=bgr, width=w, height=h, seq=_frame_seq,
                                           rtp_ts=int(_t.monotonic() * 90000), wall_ts=_t.monotonic()))
    except Exception:  # noqa: BLE001
        pass

# --- event hub ---
_ws_clients: set[WebSocket] = set()
_event_ring: deque[dict] = deque(maxlen=300)


async def emit(event: dict):
    _event_ring.append(event)
    dead = []
    for ws in list(_ws_clients):
        try:
            await ws.send_json(event)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


# ------------- lifecycle -------------
# --- 2-way call: inbound robot mic audio is fanned out to call websockets (G.711 µ-law, 8 kHz) ---
_call_clients: set[WebSocket] = set()
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def _audio_in_sink(mulaw: bytes):
    """Called from the link's audio thread; schedule a send to call clients on the event loop."""
    if not _call_clients or _MAIN_LOOP is None:
        return
    import base64
    msg = {"type": "audio", "b64": base64.b64encode(mulaw).decode()}
    for ws in list(_call_clients):
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(msg), _MAIN_LOOP)
        except Exception:  # noqa: BLE001
            pass


@app.on_event("startup")
async def _startup():
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    # Start subprocesses (native) off the event loop so a slow spawn never blocks startup.
    await asyncio.to_thread(LINK.start)
    # The Air 2 bridge link emits drive commands over the WS (the browser relays them to Agora RTM).
    if hasattr(LINK, "set_emit"):
        LINK.set_emit(emit)
    brain.start()
    # Attach the VSLAM mapper to whichever hub is live (native link's RTC hub, or the shared browser-fed hub).
    with contextlib.suppress(Exception):
        SLAM.attach(_active_hub())
    # Give the brain (curiosity coverage) + spatial skills (place tagging / go_to_place) the same pose source.
    with contextlib.suppress(Exception):
        brain.pose_provider = SLAM.map_data
    # Native links carry decoded robot-mic audio on their hub — turn it into speech for the brain (the agent
    # still gates on allow_audio_in). No-op for links without a hub / without an audio stream.
    if hasattr(LINK, "hub"):
        with contextlib.suppress(Exception):
            from ..brain.audio_sink import AudioSink
            global AUDIO_SINK
            AUDIO_SINK = AudioSink(
                on_utterance=lambda text: brain.feed_speech(text, "someone nearby", False),
                on_critical=brain.handle_critical)   # barge-in: STOP/QUIET heard while the robot is talking
            # P0-R4.3/R4.7: the kernel owns the listening decision. requested = Hear toggle; permitted =
            # effective (Hear on AND not master STOP AND not asleep). When not permitted the sink stops VAD/STT
            # AND barge-in processing (raw transport liveness is still measured for diagnostics). During master
            # STOP, TTS is already cancelled so barge-in is moot; the UI STOP/RESUME is the operator control.
            AUDIO_SINK.requested = lambda: bool(SETTINGS.snapshot().allow_audio_in)
            AUDIO_SINK.permitted = lambda: brain.safety.check_listen(SETTINGS.snapshot()).effective_enabled
            AUDIO_SINK.attach(_active_hub())
    # Video-rate looming reflex (fastest available visual collision cue): a cheap subscriber enqueues frames;
    # a worker runs optical-flow and, on looming, stops + preempts via the loop. No-op on hubless links.
    if hasattr(LINK, "hub"):
        with contextlib.suppress(Exception):
            from ..brain.reflex_vision import VisualReflex
            global VISUAL_REFLEX
            VISUAL_REFLEX = VisualReflex(on_loom=brain.on_visual_loom)
            VISUAL_REFLEX.attach(_active_hub())
    # Tap inbound robot audio for the 2-way call (coexists with the voice/STT skill). No-op on links w/o audio.
    try:
        LINK.set_audio_sink(_audio_in_sink)
    except Exception:  # noqa: BLE001
        pass
    asyncio.create_task(_telemetry_poller())
    asyncio.create_task(_audio_status_poller())
    asyncio.create_task(_daily_memory_task())


@app.on_event("shutdown")
async def _shutdown():
    with contextlib.suppress(Exception):
        await brain.emergency_stop("shutdown")   # preempt + stop before tearing down the loop
    # Tear down media workers (unsubscribe + join) so a restart leaves no duplicate subscribers / leaked threads.
    for worker in (AUDIO_SINK, VISUAL_REFLEX):
        if worker is not None and hasattr(worker, "stop"):
            with contextlib.suppress(Exception):
                await asyncio.to_thread(worker.stop)
    await brain.stop_loop()
    with contextlib.suppress(Exception):
        await asyncio.to_thread(LINK.close)


_autodock = {"active": False}


async def _maybe_autodock(t: dict):
    """Auto-recharge: when battery drops to/below the user's threshold (and we're not already charging),
    send the robot to its dock once. Resets when charging resumes. 0 disables. Uses existing dock + battery
    telemetry; goes through the link like any action (safety floor unaffected — docking isn't AI-driven motion)."""
    s = SETTINGS.snapshot()
    pct = s.autodock_pct
    if pct <= 0 or s.asleep:
        return
    batt = t.get("battery", -1)
    charging = t.get("charge") == 1
    if charging:
        _autodock["active"] = False
        return
    if isinstance(batt, (int, float)) and 0 <= batt <= pct and not _autodock["active"]:
        _autodock["active"] = True
        try:
            # go_home = dock + RELEASE our controller heartbeat, so the robot's onboard return-to-charge can
            # actually run (our keepalive otherwise suppresses its autonomy and it drains out on the floor).
            await LINK.action("go_home")
            await notify.send(emit, f"Battery {int(batt)}% — sending home + releasing control to recharge.",
                              level="warning", source="freebo-autodock")
        except Exception:  # noqa: BLE001
            pass


def _feed_imu_to_slam(t: dict) -> None:
    """Forward the Air 2's 6-axis IMU into VSLAM for visual-inertial fusion (gyro-aided yaw). Best-effort."""
    try:
        import time as _t
        imu = t.get("imu") or t.get("accel")
        gyro = t.get("gyro")
        def vec(v, keys):
            if isinstance(v, dict):
                return tuple(float(v.get(k, 0) or 0) for k in keys)
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                return (float(v[0]), float(v[1]), float(v[2]))
            return None
        a = vec(imu, ("ax", "ay", "az")) or vec(imu, ("x", "y", "z"))
        g = vec(gyro, ("gx", "gy", "gz")) or vec(gyro, ("x", "y", "z")) or (0.0, 0.0, 0.0)
        if a is not None:
            SLAM.add_imu(_t.monotonic(), a, g)
    except Exception:  # noqa: BLE001
        pass


async def _telemetry_poller():
    """Push telemetry to the UI ~every 1.5s, independent of the agent loop. Also drives the capability-status
    surface (P0-R4.6): emits the authoritative snapshot whenever it changes (a freshness threshold crossing,
    HOLD/transport change, etc.) plus a low-rate heartbeat so a UI that missed a transition event recovers."""
    last_caps = None
    last_caps_emit = 0.0
    while True:
        try:
            t = await LINK.telemetry()
            await emit({"type": "telemetry", "telemetry": t})
            await _maybe_autodock(t)
            _feed_imu_to_slam(t)
            with contextlib.suppress(Exception):
                s = SETTINGS.snapshot()
                motion_reason = brain.status_dict().get("motion_block_reason", "") or ""
                snap = brain.safety.capability_snapshot(s, motion_reason=motion_reason)
                caps = snap.get("capabilities", {})
                key = (tuple((k, caps[k]["effective"], caps[k]["reason"]) for k in sorted(caps)),
                       snap.get("master_inhibited"))
                now = time.monotonic()
                if key != last_caps or (now - last_caps_emit) >= 5.0:
                    await emit({"type": "capabilities", **snap})
                    last_caps, last_caps_emit = key, now
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(1.5)


async def _audio_status_poller():
    """Push the live mic status to the UI for the permanent listening indicator. Rate-limited: emits the
    moment the explicit state changes, ~8 Hz while active (VAD/STT/speaking, so the level meter moves), and a
    ~1 Hz heartbeat when idle — never one event per PCM packet."""
    last_state = None
    last_emit = 0.0
    while True:
        await asyncio.sleep(0.125)   # ~8 Hz tick
        if AUDIO_SINK is None or not _ws_clients or not hasattr(AUDIO_SINK, "audio_status"):
            continue
        try:
            a = AUDIO_SINK.audio_status()
        except Exception:  # noqa: BLE001
            continue
        now = time.monotonic()
        active = a["vad_active"] or a["stt_active"] or a["speaking"]
        if a["state"] != last_state or active or (now - last_emit) >= 1.0:
            await emit({"type": "audio_status", "audio": a, "ts": time.time()})
            last_state = a["state"]
            last_emit = now


async def _daily_memory_task():
    """Run the heavy-model memory cleanup ~once a day. First pass ~24h after boot."""
    import time as _t
    last = _t.time()
    while True:
        await asyncio.sleep(3600)
        if _t.time() - last < 24 * 3600:
            continue
        s = SETTINGS.snapshot()
        if not s.setup_complete:
            continue
        last = _t.time()
        res = await summarizer.summarize(SETTINGS, MEMORY)
        await emit({"type": "memory_summary", "result": res, "ts": _t.time()})


# ------------- state + settings -------------
def _state_payload() -> dict:
    tts_ok, tts_backend = tts.available()
    s = SETTINGS.snapshot()
    return {
        "settings": SETTINGS.public_dict(),
        "brain": brain.status_dict(),
        "tts": {"available": tts_ok, "backend": tts_backend, "voices": tts.list_voices(),
                "engine": s.tts_engine, "voice": s.voice},
        "identity": {
            "owner": s.owner_name,
            "present": IDENTITY.present_people(),
            "recognizer": IDENTITY._recognizer_active,
            "authority_active": IDENTITY.authority_active(s),
            "pending": IDENTITY.pending(),
        },
        "setup": {"complete": s.setup_complete},
        "audio": (AUDIO_SINK.audio_status() if (AUDIO_SINK and hasattr(AUDIO_SINK, "audio_status")) else None),
        "build": _frontend_build(),
    }


@app.get("/api/state")
async def api_state():
    return JSONResponse(_state_payload())


@app.get("/api/metrics")
async def api_metrics():
    """Per-phase brain latency (count/p50/p95/p99/mean/max/last, in ms): perceive, provider, tool, reason,
    vlm_perceive, caption, vlm_decide, omni, reflex_stop. See docs/MATURITY.md §2."""
    return JSONResponse(brain.metrics.snapshot())


@app.get("/api/telemetry")
async def api_telemetry():
    """Live robot telemetry (battery/connected/awake/video+audio frame counts/eyes/sensors). The UI gets this
    pushed over the WebSocket; this REST mirror exists for diagnostics + external health monitoring."""
    try:
        return JSONResponse(await LINK.telemetry())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=503)


@app.get("/api/diag/record_audio")
async def api_diag_record_audio(secs: float = 4.0):
    """Record a few seconds of the robot's mic from the media hub, save a WAV, and transcribe it. Definitive
    check of whether the inbound audio is real speech (vs noise) and whether STT works end to end."""
    import audioop
    import wave
    hub = _active_hub()
    chunks: list[bytes] = []
    rate = 16000

    def _cb(c):
        chunks.append(c.pcm)

    unsub = hub.subscribe_audio(_cb)
    try:
        await asyncio.sleep(max(0.5, min(15.0, secs)))
    finally:
        with contextlib.suppress(Exception):
            unsub()
    pcm = b"".join(chunks)
    if not pcm:
        return JSONResponse({"ok": False, "error": "no audio captured (is 102001 audio-on sent? robot mic streaming?)"})
    rms = audioop.rms(pcm, 2)
    path = REPO_ROOT / "data" / "captures" / "mic_probe.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
    text = ""
    try:
        gained = audioop.mul(pcm, 2, min(12.0, 3000.0 / rms)) if 0 < rms < 3000 else pcm
        model = await asyncio.to_thread(_get_whisper)
        import numpy as np
        audio = np.frombuffer(gained, dtype="<i2").astype("float32") / 32768.0
        segs, _ = await asyncio.to_thread(lambda: model.transcribe(audio, language="en", vad_filter=False))
        text = " ".join(s.text.strip() for s in segs).strip()
    except Exception as e:  # noqa: BLE001
        text = f"(transcribe error: {type(e).__name__}: {e})"
    return JSONResponse({"ok": True, "samples": len(pcm) // 2, "seconds": round(len(pcm) / 2 / rate, 2),
                         "rms": rms, "wav": str(path), "transcript": text})


@app.get("/api/diag/heard")
async def api_diag_heard():
    """Recent utterances the brain has heard (robot mic -> STT). Read-only; used by the self-test to verify
    the audio-in path end to end without needing the event WebSocket."""
    try:
        return JSONResponse({"ok": True, "heard": list(brain.buffer.transcripts)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e), "heard": []})


@app.get("/api/diag/audio")
async def api_diag_audio():
    """Per-stage AudioSink diagnostics (packet flow, VAD starts/ends, segment accept/drop, STT timings, noise
    floor, recent stage transitions). Read-only — the data the Phase 0.3 listening diagnostic is read from to
    attribute a failure to a specific stage (no audio vs VAD vs STT vs hallucination drop)."""
    try:
        dbg = AUDIO_SINK.debug() if (AUDIO_SINK and hasattr(AUDIO_SINK, "debug")) else {}
        return JSONResponse({"ok": True, "audio_sink": dbg})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e), "audio_sink": {}})


@app.post("/api/diag/audio/reset")
async def api_diag_audio_reset():
    """Start a fresh AudioSink measurement epoch (Correction 4) — call before each idle/speech window."""
    if AUDIO_SINK is None or not hasattr(AUDIO_SINK, "diag_reset"):
        return JSONResponse({"ok": False, "error": "no audio sink"})
    AUDIO_SINK.diag_reset()
    return JSONResponse({"ok": True})


@app.get("/api/diag/audio/window")
async def api_diag_audio_window():
    """Window-scoped AudioSink stats since the last reset: RMS distribution (count/min/mean/p50/p90/p95/p99/
    max), thresholds/floor, VAD + segment counts, STT + queue-wait distributions, transcripts."""
    if AUDIO_SINK is None or not hasattr(AUDIO_SINK, "diag_window"):
        return JSONResponse({"ok": False, "error": "no audio sink", "window": {}})
    return JSONResponse({"ok": True, "window": AUDIO_SINK.diag_window()})


@app.post("/api/diag/audio/capture")
async def api_diag_audio_capture(req: Request):
    """Capture the current window under a label AND append it to the calibration evidence file
    (data/test-evidence/audio_calibration.json) — the Calibrate tab's 'Stop' button. Returns the window."""
    import json as _json
    import os as _os
    import time as _t
    if AUDIO_SINK is None or not hasattr(AUDIO_SINK, "diag_window"):
        return JSONResponse({"ok": False, "error": "no audio sink", "window": {}})
    body = {}
    with contextlib.suppress(Exception):
        body = await req.json()
    label = str(body.get("label", "window"))
    win = AUDIO_SINK.diag_window()
    rec = {"label": label, "ts": _t.time(), "window": win}
    path = _os.path.join("data", "test-evidence", "audio_calibration.json")
    with contextlib.suppress(Exception):
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        data = []
        if _os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
        data.append(rec)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=2)
    return JSONResponse({"ok": True, "label": label, "window": win, "saved": path})


@app.post("/api/settings")
async def api_settings(req: Request):
    body = await req.json()
    changed = SETTINGS.update(**body)
    s = SETTINGS.snapshot()
    # P0-R4.3: a toggle represents requested state, and turning a faculty OFF must act on the live organ
    # immediately (preempt/cancel), not merely record intent. Turning back ON resumes that faculty subject to
    # the master inhibit + other gates (the loops/sinks consult the kernel).
    if "allow_motion" in changed and not s.allow_motion:
        with contextlib.suppress(Exception):
            await brain.emergency_stop("Move ability off")   # preempt active motion + stop (no latch)
    if "talk_enabled" in changed and not s.talk_enabled:
        with contextlib.suppress(Exception):
            from ..brain import audio_state
            audio_state.cancel()                              # cancel active TTS + flush queued playback
    await emit({"type": "settings", "changed": changed, "settings": SETTINGS.public_dict()})
    if {"allow_motion", "allow_video", "allow_audio_in", "talk_enabled", "allow_think",
            "asleep", "overseer"} & set(changed):
        await _emit_capabilities()
    return JSONResponse({"ok": True, "changed": changed, **_state_payload()})


async def _emit_capabilities() -> None:
    """Broadcast the ONE authoritative capability-state event (P0-R4.6). Sourced from the kernel, with the
    agent's richer motion block reason folded in."""
    s = SETTINGS.snapshot()
    motion_reason = ""
    with contextlib.suppress(Exception):
        motion_reason = brain.status_dict().get("motion_block_reason", "") or ""
    snap = brain.safety.capability_snapshot(s, motion_reason=motion_reason)
    await emit({"type": "capabilities", **snap})


@app.post("/api/estop")
async def api_estop():
    """MASTER STOP (P0-R4.1 + amendment A). The master inhibit + motion latch + generation bump are set FIRST
    (synchronously, before any await) so every faculty is blocked immediately; then the ONE stop path cancels
    TTS, invalidates reasoning, preempts the executor, and invokes the TRUE link-level estop() exactly once
    (through OverseerGate -> the real link). We do NOT call a plain LINK.stop() afterward and call it a hard
    stop. Operator video + telemetry + UI stay alive.

    Returns INDEPENDENT facts (never overloading `ok`): `ok` is the success of the whole requested operation
    (i.e. transport actually dispatched). Local inhibit/latch are reported separately and remain set on EVERY
    failure. The response is degraded (HTTP 503) when the link E-STOP did not dispatch."""
    # P0-R4 atomicity item 1: a SINGLE master-STOP transition lives inside emergency_stop()'s begin_master_stop;
    # the endpoint must NOT separately call master_inhibit() (that would double-advance the epoch). The gate is
    # asserted synchronously at the top of emergency_stop before its first await.
    res = await brain.emergency_stop("estop", cancel_tts=True, master=True)
    local_inhibit = brain.safety.is_master_inhibited()
    local_latched = brain.safety.is_latched()
    transport_ok = bool(res.get("transport_dispatch_succeeded"))
    payload = {
        "ok": transport_ok,                       # amendment A: ok=false on any degraded/failed transport
        "local_inhibit_asserted": local_inhibit,
        "local_motion_latched": local_latched,
        "transport_dispatch_succeeded": transport_ok,
        "generation": res.get("generation"),
        "transition_epoch": res.get("epoch"),
        # P0-R4 item 10: preserve the FULL nested transport result (command id, timestamps, initial-zero send,
        # retry count, rtm state, sidecar instance, generation, epoch, error) — a Boolean can't prove delivery.
        "transport_result": res.get("transport_result"),
        "error": None if transport_ok else (res.get("error") or "link E-STOP not dispatched"),
    }
    await emit({"type": "estop", **payload, "latched": local_latched, "master_inhibited": local_inhibit})
    await emit({"type": "settings", "changed": ["autonomy"], "settings": SETTINGS.public_dict()})
    await _emit_capabilities()
    # Local safety is asserted regardless; a transport failure is surfaced as a degraded response, NOT 2xx.
    return JSONResponse(payload, status_code=200 if transport_ok else 503)


@app.post("/api/resume")
async def api_resume():
    """RESUME (P0-R4.2): the explicit operator action that lifts the master STOP. It reconciles the
    link/sidecar latch+generation FIRST and stays inhibited if the sidecar reset is not acknowledged; only
    then clears the process latch + master inhibit. Each faculty restores to its own requested toggle;
    autonomy stays manual; circuit-breaker HOLD is left intact (it has its own reset)."""
    res: dict = {}
    # P0-R4 atomicity item 2: RESET is a compare-and-swap. Capture (epoch, generation) NOW; reconcile the
    # link/sidecar; then commit the desired unlatch ONLY if no transition happened meanwhile (a newer STOP
    # advances the epoch and makes this reset fail, even if the system was already inhibited).
    token = brain.safety.begin_reset()
    try:
        res = await LINK.estop_reset(generation=token.generation) or {}
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    link_ok = bool(res.get("ok", False)) if isinstance(res, dict) else False
    committed = link_ok and brain.safety.commit_reset(token)   # CAS: fails if a newer STOP intervened
    if not committed:
        await _emit_capabilities()
        reason = (res.get("error") if not link_ok else "superseded by a newer STOP (epoch advanced)") \
            or "reset not reconciled; still inhibited"
        return JSONResponse({"ok": False, "error": reason, "reconcile": res}, status_code=409)
    brain.resume()                          # clear parked state (latch+inhibit already released by the CAS)
    await emit({"type": "estop_reset", "ok": True, "latched": False, "master_inhibited": False})
    await emit({"type": "settings", "changed": [], "settings": SETTINGS.public_dict()})
    await _emit_capabilities()
    return JSONResponse({"ok": True, "resumed": True, "autonomy": "manual"})


@app.post("/api/estop/reset")
async def api_estop_reset():
    """Back-compat alias for /api/resume (the UI's old 'Reset' button). Same reconciled lift of the STOP."""
    return await api_resume()


# Remembers the autonomy mode in force before going dark, so Wake restores it (not just "auto").
_PRE_DARK_AUTONOMY: dict[str, str | None] = {"v": None}


async def _set_dark(on: bool) -> None:
    """Go dark / wake: a single kill switch. Dark = stop the model + all robot I/O (drive/audio/media) and
    release control, but keep the link session warm so Wake resumes instantly. Wake reverses it."""
    import time as _t
    s = SETTINGS.snapshot()
    if on:
        if not s.asleep:
            _PRE_DARK_AUTONOMY["v"] = s.autonomy
        SETTINGS.update(asleep=True, autonomy="manual")
        with contextlib.suppress(Exception):
            await LINK.stop()
        with contextlib.suppress(Exception):
            await LINK.connection("stop")
        await emit({"type": "status", "status": "dark", "error": None, "ts": _t.time()})
    else:
        with contextlib.suppress(Exception):
            await LINK.connection("start")
        SETTINGS.update(asleep=False, autonomy=_PRE_DARK_AUTONOMY["v"] or "auto")
    await emit({"type": "settings", "changed": ["asleep", "autonomy"], "settings": SETTINGS.public_dict()})


@app.post("/api/sleep")
async def api_sleep(req: Request):
    """Go dark (on=true) or wake (on=false): cut/restore all robot comms + model activity in one action."""
    body = await req.json()
    on = bool(body.get("on", True))
    await _set_dark(on)
    return JSONResponse({"ok": True, "asleep": on})


# ------------- overseer puppet mode (the receiver) -------------
# In overseer mode the AI brain is paralyzed (its robot-affecting calls are intercepted by OverseerGate and
# recorded as "proposals"), and a human/agent overseer drives the real robot through these endpoints. See
# autobot/robot/overseer_gate.py + the plan. Toggle the mode itself via POST /api/settings {"overseer": true}.
@app.get("/api/overseer/state")
async def api_overseer_state(since: int = 0):
    """Everything the overseer needs to puppet the robot: live telemetry, a snapshot URL, the brain's status +
    recent reasoning, and the brain's intercepted intents (proposals) since the given cursor."""
    import time as _t
    s = SETTINGS.snapshot()
    try:
        tel = await LINK.telemetry()
    except Exception as e:  # noqa: BLE001
        tel = {"ok": False, "error": str(e)}
    proposals, cursor = PROPOSALS.since(int(since or 0))
    interesting = {"thought", "tool_call", "tool_result", "speech", "observation", "motion", "proposal"}
    events = [e for e in _event_ring if e.get("type") in interesting][-50:]
    return JSONResponse({
        "ok": True,
        "ts": _t.time(),
        "overseer": s.overseer,
        "autonomy": s.autonomy,
        "asleep": s.asleep,
        "max_speed": s.max_speed,
        "max_move_duration": s.max_move_duration,
        "snapshot_url": "/api/snapshot.jpg",
        "telemetry": tel,
        "brain": brain.status_dict(),
        "proposals": [p.to_dict() for p in proposals],
        "events": events,
        "cursor": cursor,
    })


@app.post("/api/overseer/act")
async def api_overseer_act(req: Request):
    """Overseer drives the REAL robot directly, bypassing the paralyzed brain. Drive/move are clamped by the
    safety floor (speed + duration caps) but NOT autonomy-gated (the overseer is the operator, like manual
    control). Every executed command + its result is emitted as `overseer_act` so the loop is observable."""
    import time as _t
    body = await req.json()
    kind = str(body.get("kind", "")).lower()
    s = SETTINGS.snapshot()
    if s.asleep and kind not in ("stop", "connection"):
        return JSONResponse({"ok": False, "blocked": "asleep (wake first)"})
    if kind == "stop":
        res = await LINK.stop()
    elif kind in ("drive", "move", "turn"):
        d = brain.safety.check_drive(s, body.get("ly", 0.0), body.get("rx", 0.0),
                                     body.get("duration", 0.6), source="overseer")
        if not d.allowed:
            return JSONResponse({"ok": False, "blocked": d.reason})
        # 'drive' = single sustained frame (deadman stops it); 'move'/'turn' = timed burst then auto-stop.
        res = await (LINK.drive(d.ly, d.rx) if kind == "drive" else LINK.move(d.ly, d.rx, d.duration))
        res = {**res, "clamped": {"ly": d.ly, "rx": d.rx, "duration": d.duration}}
    elif kind == "action":
        res = await LINK.action(str(body.get("name", "")))
    elif kind == "eyes":
        res = await LINK.action(f"eyes_{str(body.get('animation', 'neutral')).lower()}")
    elif kind == "connection":
        res = await LINK.connection(str(body.get("state", "start")))
    elif kind in ("move_mode", "move_speed"):
        # P0-R4 amendment E: the robot's movement gear is a TYPED command (raw 103011 is banned). Air 2 only.
        rtm = getattr(LINK, "rtm", None)
        send = getattr(rtm, "_send", None)
        if not callable(send):
            return JSONResponse({"ok": False, "error": "typed RTM not available on this link"})
        if kind == "move_mode":
            cmd = {"cmd": "move_mode", "mode": int(body.get("mode", 0))}
        else:
            cmd = {"cmd": "move_speed", "speed": int(body.get("speed", 0))}
        ok = await asyncio.to_thread(send, cmd)
        res = {"ok": bool(ok), "sent": cmd}
    elif kind == "say":
        if not s.talk_enabled:
            return JSONResponse({"ok": False, "blocked": "talk disabled (UI toggle off)"})
        text = str(body.get("text", ""))
        pub = getattr(LINK, "publish_speech", None)
        if pub:
            wav = await asyncio.to_thread(tts.render_wav, text)
            res = await pub(wav) if wav else {"ok": False, "error": "tts unavailable"}
        else:
            g711 = tts.render_mulaw(text)
            res = await LINK.say_audio(g711) if g711 else await LINK.say_text(text)
    else:
        return JSONResponse({"ok": False, "error": f"unknown kind '{kind}'"}, status_code=400)
    await emit({"type": "overseer_act", "kind": kind,
                "args": {k: v for k, v in body.items() if k != "kind"}, "result": res, "ts": _t.time()})
    return JSONResponse(res)


@app.post("/api/overseer/probe")
async def api_overseer_probe(req: Request):
    """Objective motion measurement for calibration. Grabs a before frame, runs ONE clamped move
    (source=overseer), waits for it to settle, grabs an after frame, and returns motion metrics computed from
    the camera (VSLAM is unreliable on this robot, so we measure pixels directly):
      - diff:    mean abs pixel difference, normalized 0..1 (how much the whole scene changed)
      - shift_x: horizontal image shift in pixels (phase correlation) — a pure in-place yaw shows up here
      - est_yaw_deg: rough degrees from shift_x using an assumed horizontal FOV (relative, for consistency)
      - shift_y: vertical shift (pitch/translation cue)
    Body: {ly, rx, duration, settle_ms?, fov_h?}. Use ly=rx=0 to measure sensor noise only."""
    import time as _t
    body = await req.json()
    s = SETTINGS.snapshot()
    settle_ms = int(body.get("settle_ms", 1400))
    fov_h = float(body.get("fov_h", 130.0))
    before, _ = await LINK.snapshot()
    d = brain.safety.check_drive(s, body.get("ly", 0.0), body.get("rx", 0.0),
                                 body.get("duration", 0.4), source="overseer")
    moved = {"ly": d.ly, "rx": d.rx, "duration": d.duration, "allowed": d.allowed}
    if d.allowed and (d.ly or d.rx) and d.duration > 0:
        await LINK.move(d.ly, d.rx, d.duration)
    await asyncio.sleep(max(0.0, settle_ms / 1000.0))
    after, _ = await LINK.snapshot()
    from ..brain import visual_motion
    metrics = visual_motion.measure(before, after, fov_h=fov_h)
    await emit({"type": "overseer_probe", "moved": moved, "metrics": metrics, "ts": _t.time()})
    return JSONResponse({"ok": metrics.get("ok", False), "moved": moved, "metrics": metrics})


@app.post("/api/overseer/turn")
async def api_overseer_turn(req: Request):
    """Operator access to the cerebellum's CLOSED-LOOP pivot: turn a (capped) relative number of degrees in
    small, camera-measured increments. Unlike raw /api/overseer/act drive, this overcomes the deadband and
    measures the result — the fine control raw driving lacks (used for dock alignment)."""
    from ..brain import locomotion
    body = await req.json()
    s = SETTINGS.snapshot()
    res = await locomotion.turn(link=LINK, safety=brain.safety, settings=s, profile=brain.motion_profile,
                                degrees=float(body.get("degrees", 0.0)), source="overseer", emit=emit)
    return JSONResponse(res)


@app.post("/api/overseer/step")
async def api_overseer_step(req: Request):
    """Operator access to the cerebellum's confirmed forward STEP (deadband-safe, camera-verified)."""
    from ..brain import locomotion
    body = await req.json()
    s = SETTINGS.snapshot()
    res = await locomotion.step(link=LINK, safety=brain.safety, settings=s, profile=brain.motion_profile,
                                strength=float(body.get("strength", 1.0)), source="overseer", emit=emit)
    return JSONResponse(res)


@app.get("/api/calibrate")
async def api_calibrate_status():
    """Current movement-calibration profile (used to gate autonomy + size drive steps)."""
    from ..brain import motion_profile as mp
    p = mp.load()
    return JSONResponse({"ok": True, "calibrated": p is not None, "profile": p.to_dict() if p else None})


@app.post("/api/calibrate")
async def api_calibrate():
    """Run the pre-flight movement calibration: the robot does a few small test moves in open space, measures
    how much the camera view changes, and saves a motion profile. Always e-stops afterward."""
    from ..brain import motion_profile as mp
    s = SETTINGS.snapshot()
    if brain.safety.is_latched():
        return JSONResponse({"ok": False, "blocked": "estop_latched"})
    if s.asleep:
        return JSONResponse({"ok": False, "error": "asleep (wake first)"})
    if not s.allow_motion:
        return JSONResponse({"ok": False, "error": "motion disabled (enable the Move ability)"})
    try:
        res = await mp.calibrate(LINK, max_speed=s.max_speed or 0.85, emit=emit)
    finally:
        with contextlib.suppress(Exception):
            await LINK.stop()
    with contextlib.suppress(Exception):
        brain.reload_motion_profile()
    return JSONResponse(res)


@app.post("/api/tick")
async def api_tick():
    """Run one decision cycle now (handy in manual/assist for single-stepping the AI)."""
    if SETTINGS.snapshot().asleep:
        return JSONResponse({"ok": False, "error": "asleep (wake first)"})
    res = await brain.tick(force=True)
    return JSONResponse(res)


@app.post("/api/chat")
async def api_chat(req: Request):
    """Talk to the robot by text (the owner dashboard). Feeds the message in as 'heard' speech, explicitly
    addressed (bypasses the name gate), and runs one decision cycle so it can respond."""
    body = await req.json()
    text = str(body.get("text", "")).strip()
    speaker = str(body.get("speaker", "your owner")).strip() or "your owner"
    if not text:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    if brain.settings.snapshot().asleep:
        return JSONResponse({"ok": False, "error": "asleep (wake first)"})
    # Feed as a high-priority SPEECH event (addressed = bypasses the name gate). The event-driven reasoner
    # preempts idle wandering and replies promptly. In manual mode (no autonomous loop), run one cycle now.
    brain.feed_speech(text, speaker, addressed=True)
    if brain.settings.snapshot().autonomy != "auto":
        res = await brain.tick(force=True)
        return JSONResponse(res)
    return JSONResponse({"ok": True, "queued": True})


@app.post("/api/approve")
async def api_approve(req: Request):
    """Owner resolves a pending command-approval request (the obedience flow)."""
    body = await req.json()
    ok = await IDENTITY.resolve(str(body.get("id", "")), bool(body.get("approved", False)))
    return JSONResponse({"ok": ok})


# ------------- first-run setup wizard -------------
@app.get("/api/setup")
async def api_setup():
    """Setup wizard data: whether setup is done, the provider catalog (fast/heavy suggestions), and the
    current (masked) settings."""
    return JSONResponse({
        "complete": SETTINGS.snapshot().setup_complete,
        "providers": catalog_for_ui(),
        "settings": SETTINGS.public_dict(),
    })


@app.post("/api/setup/save")
async def api_setup_save(req: Request):
    """Apply wizard choices. If a known provider key is given, fill base_url from the catalog unless the
    user supplied one. Marks setup complete unless `finish` is false."""
    body = await req.json()
    prov = get_provider(str(body.get("ai_provider", "")))
    if prov and not body.get("ai_base_url") and prov.get("base_url"):
        body["ai_base_url"] = prov["base_url"]
    finish = body.pop("finish", True)
    body["setup_complete"] = bool(finish)
    changed = SETTINGS.update(**body)
    await emit({"type": "settings", "changed": changed, "settings": SETTINGS.public_dict()})
    return JSONResponse({"ok": True, "changed": changed, **_state_payload()})


# ------------- onboarding wizard (ADB provisioning + capture + connect test + owner pairing) -------------
@app.get("/api/onboarding/adb")
async def api_onboarding_adb():
    return JSONResponse(await asyncio.to_thread(ONBOARD.adb_status))


@app.post("/api/onboarding/pair")
async def api_onboarding_pair(req: Request):
    b = await req.json()
    return JSONResponse(await asyncio.to_thread(ONBOARD.adb_pair, str(b.get("host_port", "")), str(b.get("code", ""))))


@app.post("/api/onboarding/connect")
async def api_onboarding_connect(req: Request):
    b = await req.json()
    return JSONResponse(await asyncio.to_thread(ONBOARD.adb_connect, str(b.get("host_port", ""))))


@app.post("/api/onboarding/capture/start")
async def api_onboarding_capture_start(req: Request):
    b = await req.json()
    return JSONResponse(await asyncio.to_thread(
        ONBOARD.capture_start, str(b.get("serial", "")), str(b.get("apk_path", "")),
        str(b.get("package", "")), int(b.get("port", 8400))))


@app.get("/api/onboarding/capture/status")
async def api_onboarding_capture_status():
    return JSONResponse(await asyncio.to_thread(ONBOARD.capture_status))


@app.post("/api/onboarding/capture/stop")
async def api_onboarding_capture_stop():
    return JSONResponse(await asyncio.to_thread(ONBOARD.capture_stop))


@app.post("/api/onboarding/connect-test")
async def api_onboarding_connect_test():
    return JSONResponse(await ONBOARD.connect_test())


@app.post("/api/onboarding/owner")
async def api_onboarding_owner(req: Request):
    b = await req.json()
    return JSONResponse(await ONBOARD.set_owner(str(b.get("name", "")), bool(b.get("enroll", False))))


# ------------- EBO Air 2 / Max cloud session (Agora RTC video + RTM control) -------------
@app.get("/api/air2/session")
async def api_air2_session():
    """Live Agora session params for the cloud-controlled Air 2/Max. The browser uses these with the Agora
    web SDKs to show video (RTC) and send drive commands (RTM). Tokens are short-lived — fetch per use."""
    import os as _os
    from ..robot.ebo_cloud import EboCloud
    robot_id = int(_os.environ.get("EBO_ROBOT_ID", "0"))
    if not robot_id:
        return JSONResponse({"ok": False, "error": "EBO_ROBOT_ID not set"}, status_code=400)
    return JSONResponse(await EboCloud().create_session(robot_id))


@app.post("/api/air2/connected")
async def api_air2_connected(req: Request):
    """The Air 2 (cloud) UI tab reports whether it's connected to Agora (so the brain knows it can relay)."""
    b = await req.json()
    if hasattr(LINK, "set_browser"):
        LINK.set_browser(bool(b.get("connected", False)), b.get("status"))
    return JSONResponse({"ok": True})


@app.post("/api/air2/frame")
async def api_air2_frame(req: Request):
    """The Air 2 UI tab POSTs a camera frame (base64 JPEG) from the Agora RTC video so the brain can see."""
    import base64
    b = await req.json()
    data = b.get("b64", "")
    if data:
        try:
            jpeg = base64.b64decode(data)
            if hasattr(LINK, "set_frame"):
                LINK.set_frame(jpeg)
            _publish_frame(jpeg)  # fan out to UI preview + VSLAM mapper
        except Exception:  # noqa: BLE001
            pass
    return JSONResponse({"ok": True})


@app.get("/api/debug/rtm")
async def api_debug_rtm():
    """Diagnostics for the native Air2 link: RTM connectivity + recent sidecar logs (raw peer/channel msgs),
    parsed robot telemetry, and the gateway-WS message types seen. Used to map the battery/sensor fields."""
    rtm = getattr(LINK, "rtm", None)
    recv = getattr(LINK, "receiver", None)
    return JSONResponse({
        "rtm_connected": getattr(rtm, "connected", None),
        "rtm_status": getattr(rtm, "status", {}),
        # Command-delivery instrumentation (P0-R3.1): last ok/failed send, last command id + ack latency,
        # pending acks, consecutive send failures, RTM state.
        "command_delivery": rtm.debug() if (rtm and hasattr(rtm, "debug")) else {},
        "rtm_logs": rtm.recent_logs()[-30:] if rtm else [],
        "receiver_telemetry": getattr(recv, "telemetry", {}),
        "ws_types": list(getattr(recv, "_seen_types", []) or []),
        "media": recv.media_debug() if hasattr(recv, "media_debug") else {},
        "audio_sink": AUDIO_SINK.debug() if (AUDIO_SINK and hasattr(AUDIO_SINK, "debug")) else {},
    })


@app.get("/api/slam/map")
async def api_slam_map():
    """Current VSLAM pose + keyframe trail for the UI minimap. Empty/disabled if OpenCV isn't present."""
    return JSONResponse(SLAM.map_data())


@app.get("/api/selftest")
async def api_selftest(move: bool = False, talk: bool = False, only: str = "", skip: str = ""):
    """Run the live capability self-test in-process and return a JSON report (the same checks as
    scripts/robot_selftest.py). Safe by default: no driving/talk, never the interactive 'hear' check, and it
    always restores settings + e-stops afterward. Pass ?move=1 to include the motion + autonomy checks."""
    from ..diagnostics.checks import Options
    from ..diagnostics.runner import selftest as _selftest
    s = SETTINGS.snapshot()
    base = f"http://127.0.0.1:{s.port}"
    opts = Options(allow_move=bool(move), test_talk=bool(talk), test_hear=False, on_progress=lambda _m: None)
    only_l = [x.strip() for x in only.split(",") if x.strip()] or None
    skip_l = [x.strip() for x in skip.split(",") if x.strip()] or None
    report = await _selftest(base, opts, only=only_l, skip=skip_l)
    return JSONResponse(report)


@app.get("/api/video/preview.mjpeg")
async def api_video_preview():
    """Server-side MJPEG of the live camera (from the media hub) — the UI's video when media is native
    (no browser Agora). ~8 fps, downscaled; closes when the client disconnects."""
    async def gen():
        last = -1
        while True:
            hub = _active_hub()
            f = hub.latest_frame()
            if f is not None and f.seq != last:
                last = f.seq
                jpeg = hub.latest_video_jpeg(max_w=720, quality=70)
                if jpeg:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
            await asyncio.sleep(0.1)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


# ------------- voice: server-side Whisper STT (in) + Piper TTS WAV (out, for the robot speaker) -------------
_whisper = None


def _get_whisper():
    global _whisper
    if _whisper is None:
        import os as _os
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(_os.environ.get("AUTOBOT_STT_MODEL", "base"), device="cpu", compute_type="int8")
    return _whisper


@app.post("/api/voice/stt")
async def api_voice_stt(req: Request):
    """Transcribe a recorded audio blob (the robot's mic) with local Whisper. Returns {text}."""
    import os as _os
    import tempfile
    if not SETTINGS.snapshot().allow_audio_in:
        return JSONResponse({"ok": False, "error": "listening disabled (Control toggle)"})
    data = await req.body()
    if not data:
        return JSONResponse({"ok": False, "error": "no audio"})
    try:
        model = await asyncio.to_thread(_get_whisper)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"faster-whisper unavailable: {e} (pip install faster-whisper)"})
    fd, path = tempfile.mkstemp(suffix=".webm"); _os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(data)

        def _tx():
            # vad_filter=False: faster-whisper's silero VAD needs onnxruntime, which can crash under numpy 2.
            segs, _info = model.transcribe(path, vad_filter=False)
            return "".join(s.text for s in segs).strip()
        text = await asyncio.to_thread(_tx)
        return JSONResponse({"ok": True, "text": text})
    finally:
        try:
            _os.remove(path)
        except OSError:
            pass


@app.get("/api/voice/say")
async def api_voice_say(text: str = ""):
    """Render text to a WAV with the configured TTS (Piper). The Air 2 tab publishes this into the Agora call
    so the robot's own speaker plays it. Gated by the audio-out toggle (talk_enabled) — except an explicit
    UI test tone (test=1), which is allowed so you can verify the speaker even with talk off."""
    if not text.strip():
        return Response(content=b"", status_code=400)
    if not SETTINGS.snapshot().talk_enabled and text.strip() != "__test__":
        return Response(content=b"", status_code=403, headers={"X-Reason": "audio output disabled"})
    if text.strip() == "__test__":
        text = "Audio test. If you can hear this through the robot, two way audio works."
    wav = await asyncio.to_thread(tts.render_wav, text)
    if not wav:
        return Response(content=b"", status_code=503, headers={"X-Reason": "tts unavailable"})
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.get("/api/memory")
async def api_memory():
    """The memory browser feed: curated long-term facts, recent sightings, and recent daily notes."""
    facts = [f.to_dict() for f in MEMORY.all_facts()]
    facts.sort(key=lambda d: d.get("ts", 0), reverse=True)
    return JSONResponse({
        "ok": True,
        "facts": facts,
        "sightings": MEMORY.recent_sightings(40)[::-1],
        "recent": MEMORY.recent_events(days=2)[-60:][::-1],
        "embeddings": __import__("autobot.brain.embeddings", fromlist=["embeddings_enabled"]).embeddings_enabled(),
    })


@app.post("/api/memory/forget")
async def api_memory_forget(req: Request):
    """Delete remembered facts matching a phrase (UI memory browser)."""
    body = await req.json()
    n = await asyncio.to_thread(MEMORY.forget, str(body.get("query", "")))
    return JSONResponse({"ok": True, "forgot": n})


@app.get("/api/tasks")
async def api_tasks():
    """List the robot's scheduled tasks/reminders (UI tasks panel)."""
    from dataclasses import asdict
    tasks = [asdict(t) | {"schedule": t.schedule_label()} for t in brain.tasks.list()]
    return JSONResponse({"ok": True, "tasks": tasks})


@app.post("/api/tasks/add")
async def api_tasks_add(req: Request):
    """Add a scheduled task/reminder. Provide one of in_seconds, daily_time ('HH:MM'), or every_seconds."""
    b = await req.json()
    text = str(b.get("text", "")).strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text required"})
    t = brain.tasks.add(text, in_seconds=b.get("in_seconds"), daily_time=b.get("daily_time") or None,
                        every_seconds=b.get("every_seconds"))
    return JSONResponse({"ok": True, "task_id": t.id, "schedule": t.schedule_label()})


@app.post("/api/tasks/cancel")
async def api_tasks_cancel(req: Request):
    b = await req.json()
    return JSONResponse({"ok": brain.tasks.cancel(str(b.get("id", "")))})


@app.post("/api/memory/summarize")
async def api_memory_summarize():
    """Trigger the heavy-model daily memory cleanup now (also runs automatically ~once a day)."""
    res = await summarizer.summarize(SETTINGS, MEMORY)
    await emit({"type": "memory_summary", "result": res, "ts": __import__("time").time()})
    return JSONResponse(res)


@app.post("/api/agora/capture")
async def api_agora_capture(req: Request):
    """Reverse-engineering harness: append captured Agora signaling frames (from the browser WS wrapper) to
    data/captures/agora_signaling.jsonl, so we can reconstruct the join/signaling protocol natively."""
    import pathlib
    try:
        body = await req.body()
        d = pathlib.Path("data/captures"); d.mkdir(parents=True, exist_ok=True)
        with (d / "agora_signaling.jsonl").open("ab") as f:
            f.write(body.rstrip() + b"\n")
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({"ok": True})


@app.post("/api/memory/clear")
async def api_memory_clear():
    """Wipe all memory (facts + daily notes + sightings + place log) for a clean slate."""
    res = MEMORY.clear()
    try:
        brain.history.clear()
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse(res)


# ------------- manual controls (clamped, but not autonomy-gated) -------------
@app.post("/api/control")
async def api_control(req: Request):
    body = await req.json()
    kind = body.get("kind")
    s = SETTINGS.snapshot()
    if kind == "stop":
        # Manual takeover: unified emergency stop (cancels TTS + preempts the active action + stops).
        with contextlib.suppress(Exception):
            await brain.emergency_stop("manual stop", cancel_tts=True)
        return JSONResponse(await LINK.stop())
    # When dark, refuse anything that talks to the robot except stop/connection (Wake re-enables I/O).
    if s.asleep and kind in ("drive", "move", "say", "action"):
        return JSONResponse({"ok": False, "blocked": "asleep (wake first)"})
    if kind in ("drive", "move"):
        # P0-R4.3: the Move toggle governs ALL movement — manual no longer bypasses it. (A separately named
        # operator override could be added here later; there is intentionally none today.)
        if not s.allow_motion:
            return JSONResponse({"ok": False, "blocked": "Move ability off"})
        # Manual motion preempts + cancels any active AI action and clears any circuit-breaker HOLD (the human
        # is now in control). Manual control stays direct (only speed/duration-clamped by the safety floor).
        with contextlib.suppress(Exception):
            await brain.executor.preempt("manual takeover")
            brain.executor.reset_breaker()
        d = brain.safety.check_drive(s, body.get("ly", 0.0), body.get("rx", 0.0),
                                     body.get("duration", 0.4), source="manual")
        if not d.allowed:
            return JSONResponse({"ok": False, "blocked": d.reason})
        if kind == "drive":
            return JSONResponse(await LINK.drive(d.ly, d.rx))
        return JSONResponse(await LINK.move(d.ly, d.rx, d.duration))
    if kind == "action":
        name = str(body.get("name", ""))
        return JSONResponse(await LINK.action(name))
    if kind == "connection":
        return JSONResponse(await LINK.connection(str(body.get("state", "start"))))
    if kind == "say":
        text = str(body.get("text", ""))
        # Route through the unified SpeechService so this path ALSO arms the echo gate, sanitizes reserved
        # words, and retains a cancellable playback id (so barge-in/STOP can cancel a manually-triggered clip
        # exactly like the brain's own speech). check_say enforces the talk toggle / quiet window.
        return JSONResponse(await brain.speech.speak(text, check_say=True, safety=brain.safety))
    return JSONResponse({"ok": False, "error": f"unknown control kind '{kind}'"}, status_code=400)


@app.post("/api/debug/param")
async def api_debug_param(req: Request):
    """Raw PARAM_SET sender for eye-animation discovery (robot/native/probe_eyes.py). Native link only."""
    send = getattr(LINK, "send_rdt", None)
    if send is None:
        return JSONResponse({"ok": False, "error": "not available on this link"}, status_code=501)
    body = await req.json()
    frame = param_set_frame(str(body.get("group", "")), str(body.get("key", "")), float(body.get("value", 0.0)))
    await asyncio.to_thread(send, frame)
    return JSONResponse({"ok": True, "sent": f"{body.get('group')}/{body.get('key')}={body.get('value')}"})


# ------------- media proxy (same-origin for the browser) -------------
@app.get("/api/snapshot.jpg")
async def api_snapshot():
    data, err = await LINK.snapshot()
    if data is None:
        return Response(content=b"", status_code=503, headers={"X-Reason": err or "unavailable"})
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/whep")
async def whep(request: Request):
    upstream = LINK.whep_upstream
    if not upstream:
        return Response(content=b"no video on this link", status_code=501)
    offer = await request.body()
    headers = {"Content-Type": "application/sdp", **LINK.stream_auth_header()}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(upstream, content=offer, headers=headers)
        return Response(content=r.content, status_code=r.status_code,
                        media_type=r.headers.get("Content-Type", "application/sdp"))
    except Exception as e:  # noqa: BLE001
        return Response(content=str(e).encode(), status_code=502)


@app.get("/hls/{path:path}")
async def hls(path: str, request: Request):
    base = LINK.hls_base
    if not base:
        return Response(content=b"no video on this link", status_code=501)
    url = f"{base}/{path}"
    q = request.url.query
    if q:
        url += "?" + q
    try:
        client = httpx.AsyncClient(timeout=30)
        req = client.build_request("GET", url, headers=LINK.stream_auth_header())
        r = await client.send(req, stream=True)
    except Exception as e:  # noqa: BLE001
        return Response(content=str(e).encode(), status_code=502)

    async def gen():
        try:
            async for chunk in r.aiter_bytes():
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(gen(), status_code=r.status_code,
                             media_type=r.headers.get("Content-Type", "application/octet-stream"))


# ------------- WebSocket -------------
@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_json({"type": "hello", **_state_payload()})
        for ev in list(_event_ring)[-80:]:
            await ws.send_json(ev)
        while True:
            # we don't need client messages, but keep the socket alive and drain pings
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        _ws_clients.discard(ws)


@app.websocket("/ws/call")
async def ws_call(ws: WebSocket):
    """2-way call audio bridge. Browser sends {type:'say_audio', b64:<G.711 µ-law 8kHz>} for push-to-talk;
    server forwards to the robot speaker (talk-gated). Inbound robot mic audio is pushed back as {type:'audio'}.
    Live video is the existing WHEP stream; this socket is audio-only."""
    import base64
    import json as _json
    await ws.accept()
    _call_clients.add(ws)
    try:
        await ws.send_json({"type": "call_hello", "talk_enabled": SETTINGS.snapshot().talk_enabled})
        while True:
            raw = await ws.receive_text()
            try:
                msg = _json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if msg.get("type") == "say_audio":
                s = SETTINGS.snapshot()
                if not s.talk_enabled:
                    await ws.send_json({"type": "blocked", "reason": "talk disabled (enable it in Config)"})
                    continue
                g711 = base64.b64decode(msg.get("b64", "") or "")
                if g711:
                    await LINK.say_audio(g711, "mulaw")
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        _call_clients.discard(ws)


# ------------- UI -------------
if WEBUI_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(WEBUI_DIST / "assets")), name="assets")


@app.get("/", response_class=HTMLResponse)
async def index():
    if WEBUI_DIST.exists() and (WEBUI_DIST / "index.html").exists():
        return (WEBUI_DIST / "index.html").read_text(encoding="utf-8")
    if FALLBACK_HTML.exists():
        return FALLBACK_HTML.read_text(encoding="utf-8")
    return "<h1>FreeBo</h1><p>UI not built. Run <code>cd webui && npm install && npm run build</code>.</p>"


def _maybe_start_mqtt():
    """Optional Home Assistant MQTT — native link only, and only if EBO_MQTT_HOST is set. MQTT runs on its
    own threads and pokes the native link's synchronous helpers directly (no event loop involved)."""
    import os
    host = os.environ.get("EBO_MQTT_HOST")
    if not host or SETTINGS.snapshot().robot_link != "native":
        return
    do_action = getattr(LINK, "_do_action", None)
    do_move = getattr(LINK, "_do_move", None)
    if do_action is None or do_move is None:
        return
    try:
        from ..robot.mqtt import EBOMqtt
        di = {"name": os.environ.get("EBO_NAME", "EBO-SE"), "model": "EBO SE",
              "uid": os.environ.get("EBO_UID", ""), "ip": os.environ.get("EBO_ROBOT_IP", "")}
        EBOMqtt(LINK, do_action, do_move, di, host, int(os.environ.get("EBO_MQTT_PORT", "1883")),
                os.environ.get("EBO_MQTT_USER") or None, os.environ.get("EBO_MQTT_PASS") or None)
        print(f"[autobot] MQTT -> {host}", flush=True)
    except Exception as e:  # noqa: BLE001
        print("[autobot] MQTT error:", e, flush=True)


def main():
    s = SETTINGS.snapshot()
    # P0-R4.8: loudly warn when the production frontend is absent or stale (source newer than the built
    # bundle) so we never silently serve an old UI. (Build-on-deploy lives in scripts/bootstrap.py.)
    b = _frontend_build()
    if not b.get("dist_present"):
        print("[autobot] WARNING: webui/dist missing — UI not built. Run: cd webui && npm install && npm run build",
              flush=True)
    elif b.get("stale"):
        print("[autobot] WARNING: webui/dist is STALE (source newer than the built bundle). Rebuild: "
              "cd webui && npm run build", flush=True)
    else:
        print(f"[autobot] frontend: asset={b.get('asset')} sha256={b.get('asset_sha256')} "
              f"src_commit={b.get('source_commit')}", flush=True)
    _maybe_start_mqtt()
    uvicorn.run(app, host=s.host, port=s.port, log_level="info")


if __name__ == "__main__":
    main()
