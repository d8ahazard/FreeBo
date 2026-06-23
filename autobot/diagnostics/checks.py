"""Capability checks — one async function per thing the robot must be able to do.

Each returns a `CheckResult` (status + human detail + a remediation hint + machine-readable evidence). They
drive the live robot through `AppClient` (manual control is clamped but not autonomy-gated, so motion checks
work in any mode). Checks are intentionally small and independent; the runner orders them, manages settings,
and guarantees the robot is stopped afterwards.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .client import AppClient
from .motion import classify_motion, frame_diff, pose_delta


class Status:
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"
    SYMBOL = {"PASS": "✔", "FAIL": "✘", "WARN": "!", "SKIP": "-"}


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    hint: str = ""
    evidence: dict = field(default_factory=dict)
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "hint": self.hint, "evidence": self.evidence, "elapsed": round(self.elapsed, 2)}


@dataclass
class Options:
    """Knobs the runner passes to checks."""
    allow_move: bool = True            # actually command the robot to move (motion + rotate checks)
    test_talk: bool = False            # play TTS through the robot speaker
    test_hear: bool = False            # interactive: ask the operator to speak
    speed: float = 0.5                 # drive magnitude for motion checks (clamped by the app's max_speed too)
    move_duration: float = 1.5         # forward burst seconds
    turn_duration: float = 2.5         # rotate burst seconds (the robot turns slowly — needs a longer turn)
    settle: float = 0.8                # extra wait after a burst before the "after" snapshot
    hear_timeout: float = 20.0         # how long to wait for a spoken phrase to be transcribed
    on_progress: Callable[[str], None] = lambda _m: None
    ask: Optional[Callable[[str], Awaitable[None]]] = None   # await operator acknowledgement (interactive)


def _r(name: str, status: str, t0: float, detail: str = "", hint: str = "", **ev) -> CheckResult:
    return CheckResult(name=name, status=status, detail=detail, hint=hint, evidence=ev,
                       elapsed=time.time() - t0)


# --- 1. connection / session -------------------------------------------------
async def check_connection(c: AppClient, o: Options) -> CheckResult:
    t0 = time.time()
    try:
        tel = await c.telemetry()
    except Exception as e:  # noqa: BLE001
        return _r("connection", Status.FAIL, t0, f"telemetry unreachable: {e}",
                  "Is the app running? Try a different --app URL.")
    connected = bool(tel.get("connected"))
    awake = bool(tel.get("awake"))
    via = tel.get("via", "?")
    batt = tel.get("battery", -1)
    vframes = tel.get("video_frames", tel.get("frames_received"))
    ev = {"connected": connected, "awake": awake, "via": via, "battery": batt, "video_frames": vframes}
    if not connected:
        return _r("connection", Status.FAIL, t0, f"robot not connected (via {via})",
                  "Check the robot is powered/online and the cloud session is valid (GET /api/debug/rtm).", **ev)
    if isinstance(batt, (int, float)) and 0 <= batt <= 8:
        return _r("connection", Status.WARN, t0, f"connected but battery critically low ({batt}%)",
                  "Dock and charge — the robot can't drive on a near-dead battery.", **ev)
    return _r("connection", Status.PASS, t0, f"connected via {via}, battery {batt}%", **ev)


# --- 2. video freshness ------------------------------------------------------
async def check_video(c: AppClient, o: Options) -> CheckResult:
    t0 = time.time()
    jpeg1, err = await c.snapshot()
    if jpeg1 is None:
        return _r("video", Status.FAIL, t0, f"no camera frame ({err})",
                  "The brain is blind without video. Check the RTC receiver (GET /api/debug/rtm).", error=err)
    try:
        f0 = (await c.telemetry()).get("video_frames", 0)
    except Exception:  # noqa: BLE001
        f0 = 0
    await asyncio.sleep(2.0)
    jpeg2, _ = await c.snapshot()
    f1 = (await c.telemetry()).get("video_frames", 0)
    fd = frame_diff(jpeg1, jpeg2 or b"")
    advanced = isinstance(f0, (int, float)) and isinstance(f1, (int, float)) and f1 > f0
    ev = {"bytes": len(jpeg1), "video_frames_before": f0, "video_frames_after": f1,
          "frame_diff": round(fd, 4) if fd is not None else None}
    if not advanced and (fd is None or fd < 0.001):
        return _r("video", Status.WARN, t0, "got a frame but the stream looks frozen (count not advancing)",
                  "Frames aren't flowing — the receiver may be stalled; it should self-rejoin within ~6s.", **ev)
    return _r("video", Status.PASS, t0, f"live video ({len(jpeg1)//1024}KB frames, count advancing)", **ev)


# --- 3 + 4. move + rotate with motion confirmation ---------------------------
async def _measure_baseline(c: AppClient, *, samples: int = 5,
                            gap: float = 0.35) -> tuple[bytes | None, float | None, str | None]:
    """Camera diff while the robot is STILL — the per-scene noise floor a real move must beat. Uses the
    MEDIAN of several consecutive diffs so one glitchy/exposure-jump frame can't poison the floor (we saw a
    lone 0.25 spike against a true ~0.001 noise). Returns the last frame as the pre-move reference too."""
    frames: list[bytes] = []
    err: str | None = None
    for _ in range(samples):
        f, e = await c.snapshot()
        if f is not None:
            frames.append(f)
        else:
            err = e
        await asyncio.sleep(gap)
    if not frames:
        return None, None, err
    diffs = [d for d in (frame_diff(frames[i], frames[i + 1]) for i in range(len(frames) - 1))
             if d is not None]
    base = statistics.median(diffs) if diffs else None
    return frames[-1], base, None


async def _after_move_diff(c: AppClient, before: bytes) -> tuple[float | None, bytes | None]:
    """Change vs the pre-move reference, taken as the MIN of two post-move frames — so a single glitch frame
    can't falsely read as motion (a real move sustains the change across both)."""
    a1, _ = await c.snapshot()
    a2, _ = await c.snapshot()
    diffs = [d for d in (frame_diff(before, a1 or b""), frame_diff(before, a2 or b"")) if d is not None]
    return (min(diffs) if diffs else None), (a2 or a1)


async def _drive_and_confirm(c: AppClient, o: Options, *, name: str, ly: float, rx: float,
                             duration: float, expected: str) -> CheckResult:
    t0 = time.time()
    if not o.allow_move:
        return _r(name, Status.SKIP, t0, "movement checks disabled (--no-move)")
    # 1) Still-noise floor: how much does the camera change while NOT moving (compression + scene jitter)?
    o.on_progress(f"  {name}: measuring still-camera noise floor…")
    before, baseline, err = await _measure_baseline(c)
    if before is None:
        return _r(name, Status.SKIP, t0, f"no camera frame to confirm motion ({err})",
                  "Fix the video check first — motion confirmation needs the camera.")
    slam_before = await _safe_slam(c)

    # 2) FIRE the command — and confirm the link actually accepted/sent it.
    base_str = f"{baseline:.4f}" if baseline is not None else "n/a"
    o.on_progress(f"  {name}: firing move (ly={ly} rx={rx} {duration}s); still-noise={base_str}")
    res = await c.control(kind="move", ly=ly, rx=rx, duration=duration)
    if res.get("blocked"):
        return _r(name, Status.FAIL, t0, f"command BLOCKED — never fired ({res['blocked']})",
                  "Manual moves are only speed/duration-clamped; a block means a hard gate or bad input.",
                  fired=False, response=res)
    if not res.get("ok", False):
        return _r(name, Status.FAIL, t0, f"command NOT accepted by the link — never fired ({res})",
                  "The link rejected the drive (RTM down? session expired?). See GET /api/debug/rtm.",
                  fired=False, response=res)

    # 3) Was it ACTED UPON? Truth = the robot's own camera changing more than the still-noise floor.
    await asyncio.sleep(duration + o.settle)
    await c.stop()
    fd, _ = await _after_move_diff(c, before)
    pd = pose_delta(slam_before, await _safe_slam(c))
    m = classify_motion(fd, expected=expected, baseline=baseline, pose=pd)
    ev = {"fired": True, "motion_state": m.state, "frame_diff": m.frame_diff,
          "baseline_diff": round(baseline, 4) if baseline is not None else None,
          "slam_pose": pd, "motion_detail": m.detail}
    if m.state == "moved":
        return _r(name, Status.PASS, t0, f"command FIRED and robot MOVED — {m.detail}", **ev)
    if m.state == "blocked":
        return _r(name, Status.WARN, t0, f"command fired but robot barely moved — {m.detail}",
                  "Sent + accepted, but the view changed only slightly — obstacle/wall/dock/low traction, "
                  "or the robot only twitched.", **ev)
    if m.state == "stuck":
        return _r(name, Status.FAIL, t0, "command FIRED but the robot did NOT move",
                  "The drive was sent and accepted, but the camera view didn't change beyond still-noise. On "
                  "Air 2 the robot only acts on drive while an RTC call is active — also check wheels, that it "
                  "isn't docked/resting, and the RTM keepalive (GET /api/debug/rtm).", **ev)
    return _r(name, Status.WARN, t0, f"could not verify motion ({m.detail})",
              "No decodable camera frame — install opencv-python so motion-confirm can read frames.", **ev)


async def check_move(c: AppClient, o: Options) -> CheckResult:
    return await _drive_and_confirm(c, o, name="move_forward", ly=o.speed, rx=0.0,
                                    duration=o.move_duration, expected="translate")


async def check_rotate(c: AppClient, o: Options) -> CheckResult:
    # Use a firm turn rate (clamped by the app's max_speed) — a gentle turn barely changes the view.
    return await _drive_and_confirm(c, o, name="rotate", ly=0.0, rx=max(o.speed, 0.6),
                                    duration=o.turn_duration, expected="rotate")


# --- 5. eyes -----------------------------------------------------------------
async def check_eyes(c: AppClient, o: Options) -> CheckResult:
    t0 = time.time()
    res = await c.control(kind="action", name="eyes_happy")
    if not res.get("ok", False):
        return _r("eyes", Status.FAIL, t0, f"eye command rejected: {res}",
                  "Eyes use the RTM emote path; a failure usually means RTM control is down.", response=res)
    await asyncio.sleep(0.6)
    eyes = (await c.telemetry()).get("eyes_animation")
    # be polite: return to neutral
    await c.control(kind="action", name="eyes_neutral")
    if eyes == "happy":
        return _r("eyes", Status.PASS, t0, "set eyes to 'happy' and telemetry confirmed it", eyes=eyes)
    return _r("eyes", Status.WARN, t0, f"eye command accepted but telemetry shows '{eyes}'",
              "Command went through; telemetry may just not echo eye state on this unit.", eyes=eyes)


# --- 6. talk through the robot speaker ---------------------------------------
async def check_talk(c: AppClient, o: Options) -> CheckResult:
    t0 = time.time()
    # First: does the TTS pipeline render audio at all? (Independent of the robot link.)
    tts_ok, tts_info = await c.voice_say("Diagnostic talk test.")
    if not o.test_talk:
        return _r("talk", Status.SKIP, t0,
                  f"robot talkback not exercised (--talk to enable). TTS render: {'ok' if tts_ok else 'FAIL'}",
                  "Pass --talk to actually play audio through the robot speaker.",
                  tts_ok=tts_ok, tts=tts_info)
    if not tts_ok:
        return _r("talk", Status.FAIL, t0, f"TTS render failed ({tts_info})",
                  "Install a Piper voice (scripts/get_voice.py) or an OS TTS engine; ffmpeg is required.",
                  tts_ok=False)
    o.on_progress("  asking the robot to speak — listen to the robot's speaker…")
    res = await c.control(kind="say", text="Hello, this is a diagnostic talk test.")
    available = res.get("available")
    if res.get("ok"):
        return _r("talk", Status.PASS, t0, "robot accepted talkback audio (listen to confirm the speaker)",
                  tts_ok=True, response=res)
    if res.get("blocked"):
        return _r("talk", Status.FAIL, t0, f"talk blocked: {res['blocked']}",
                  "Enable talk (talk_enabled) — the runner sets it, so this means a deeper gate.", response=res)
    return _r("talk", Status.FAIL, t0, f"robot talkback unavailable: {res.get('error', res)}",
              "Native Air 2 talkback must publish TTS audio into the Agora RTC call.",
              tts_ok=True, available=available, response=res)


# --- 7. hear (interactive) ---------------------------------------------------
async def check_hear(c: AppClient, o: Options) -> CheckResult:
    t0 = time.time()
    if not o.test_hear:
        return _r("hear", Status.SKIP, t0, "audio-in not exercised (--hear to enable, requires you to speak)")
    # Confirm audio is actually arriving from the robot mic.
    a0 = (await c.telemetry()).get("audio_frames", 0)
    await asyncio.sleep(3.0)
    a1 = (await c.telemetry()).get("audio_frames", 0)
    audio_flowing = isinstance(a0, (int, float)) and isinstance(a1, (int, float)) and a1 > a0
    before = await _safe_heard(c)
    n_before = len(before)
    o.on_progress(f"  SPEAK NOW (clearly, near the robot). Waiting up to {int(o.hear_timeout)}s for a transcript…")
    if o.ask:
        # let the operator get ready; non-blocking acknowledgement
        try:
            await o.ask("Press Enter, then speak a short sentence to the robot…")
        except Exception:  # noqa: BLE001
            pass
    deadline = time.time() + o.hear_timeout
    heard_text = None
    while time.time() < deadline:
        cur = await _safe_heard(c)
        if len(cur) > n_before:
            heard_text = cur[-1].get("text", "")
            break
        await asyncio.sleep(1.0)
    ev = {"audio_flowing": audio_flowing, "audio_frames_before": a0, "audio_frames_after": a1,
          "heard": heard_text}
    if heard_text:
        return _r("hear", Status.PASS, t0, f"transcribed: \"{heard_text[:60]}\"", **ev)
    if not audio_flowing:
        return _r("hear", Status.FAIL, t0, "no audio arriving from the robot mic",
                  "Robot mic audio isn't reaching the app — check the RTC audio stream (Opus) is subscribed.",
                  **ev)
    return _r("hear", Status.WARN, t0, "audio is flowing but nothing was transcribed",
              "Audio reaches the app but STT produced no text. Install faster-whisper, speak louder/closer, "
              "and confirm allow_audio_in is on.", **ev)


# --- 8. autonomy loop --------------------------------------------------------
async def check_autonomy(c: AppClient, o: Options) -> CheckResult:
    t0 = time.time()
    if not o.allow_move:
        return _r("autonomy", Status.SKIP, t0, "movement disabled (--no-move) — skipping the drive cycle",
                  "The autonomy check forces a real decision that can drive; enable movement to run it.")
    # Enable the autonomous loop only now (so it can't idle-drive while other checks run); restored by runner.
    try:
        await c.settings(autonomy="auto", allow_think=True, allow_motion=True, allow_video=True)
        await asyncio.sleep(0.2)
    except Exception:  # noqa: BLE001
        pass
    o.on_progress("  autonomy: measuring still-camera noise floor…")
    before, baseline, _ = await _measure_baseline(c)
    slam_before = await _safe_slam(c)
    o.on_progress("  autonomy: forcing one autonomous decision cycle…")
    try:
        res = await c.tick()
    except Exception as e:  # noqa: BLE001
        return _r("autonomy", Status.FAIL, t0, f"decision cycle errored: {e}",
                  "The brain raised during a tick — check the AI/VLM provider is up and configured.")
    if not res.get("ok", True) and res.get("error"):
        return _r("autonomy", Status.FAIL, t0, f"brain not ready: {res.get('error')}",
                  "Finish setup and point the brain at a working AI/VLM endpoint.", response=res)
    actions = res.get("actions", []) or []
    drove = any(a.get("name") == "drive" for a in actions) or res.get("action") in (
        "forward", "back", "backward", "left", "right")
    if res.get("skipped"):
        return _r("autonomy", Status.WARN, t0, f"cycle ran but was skipped ({res['skipped']})",
                  "The robot was resting/blind/asleep — the brain correctly declined to act.", response=res)
    if not drove:
        # Healthy reasoning, just no motion this cycle (it might wait/look/speak).
        return _r("autonomy", Status.WARN, t0,
                  f"brain ran a full cycle but chose not to drive (action={res.get('action')})",
                  "Healthy reasoning, just no motion this cycle. Re-run, or check the scene is drivable.",
                  actions=[a.get("name") for a in actions], response=res)
    # The brain DID issue a drive — verify the robot physically moved (camera, not VSLAM).
    if before is None:
        return _r("autonomy", Status.WARN, t0, "brain drove, but no camera frame to verify motion",
                  "Fix the video check so autonomous motion can be confirmed.", response=res)
    await asyncio.sleep(o.move_duration + o.settle)
    await c.stop()
    fd, _ = await _after_move_diff(c, before)
    m = classify_motion(fd, baseline=baseline,
                        pose=pose_delta(slam_before, await _safe_slam(c)))
    ev = {"motion_state": m.state, "frame_diff": m.frame_diff,
          "baseline_diff": round(baseline, 4) if baseline is not None else None,
          "actions": [a.get("name") for a in actions]}
    if m.state == "moved":
        return _r("autonomy", Status.PASS, t0, f"brain drove autonomously AND the robot moved — {m.detail}", **ev)
    if m.state == "stuck":
        return _r("autonomy", Status.FAIL, t0, "brain fired drive(s) but the robot did NOT move",
                  "This is the failure you described: the AI commands motion the robot never acts on. On Air 2 "
                  "the robot ignores drive unless an RTC call is active — check /api/debug/rtm + the sidecar.",
                  **ev)
    return _r("autonomy", Status.WARN, t0, f"brain drove but motion was only partial — {m.detail}",
              "Obstructed, or the robot only twitched. Re-run in open space.", **ev)


# --- 9. VSLAM ---------------------------------------------------------------
async def check_vslam(c: AppClient, o: Options) -> CheckResult:
    t0 = time.time()
    m = await _safe_slam(c)
    if not m:
        return _r("vslam", Status.SKIP, t0, "no SLAM data endpoint")
    if not m.get("enabled"):
        return _r("vslam", Status.SKIP, t0, "VSLAM disabled (OpenCV/NumPy not installed)",
                  "pip install opencv-python numpy to enable visual odometry + the minimap.")
    frames = m.get("frames", 0)
    kf = m.get("keyframes", 0)
    pose = m.get("pose", {})
    ev = {"frames": frames, "keyframes": kf, "pose": pose}
    if frames and frames > 0:
        return _r("vslam", Status.PASS, t0,
                  f"VSLAM processing video ({frames} frames, {kf} keyframes). "
                  f"Note: monocular + no Air 2 IMU, so pose is odometry-grade (relative).", **ev)
    return _r("vslam", Status.WARN, t0, "VSLAM enabled but hasn't processed frames yet",
              "Needs the live video stream — fix the video check first.", **ev)


# --- helpers ----------------------------------------------------------------
async def _safe_slam(c: AppClient) -> dict:
    try:
        return await c.slam_map()
    except Exception:  # noqa: BLE001
        return {}


async def _safe_heard(c: AppClient) -> list[dict]:
    try:
        return await c.heard()
    except Exception:  # noqa: BLE001
        return []


# Ordered registry. `mutates` lists the settings keys a check needs flipped on (the runner sets+restores).
@dataclass
class CheckSpec:
    id: str
    fn: Callable[[AppClient, Options], Awaitable[CheckResult]]
    desc: str
    needs: dict = field(default_factory=dict)   # settings the runner must set before this check


ALL_CHECKS: list[CheckSpec] = [
    CheckSpec("connection", check_connection, "Robot online + session up"),
    CheckSpec("video", check_video, "Live camera frames flowing"),
    CheckSpec("eyes", check_eyes, "Eye expression control"),
    CheckSpec("move", check_move, "Drive forward + confirm motion via video/VSLAM"),
    CheckSpec("rotate", check_rotate, "Rotate in place + confirm via video/VSLAM"),
    CheckSpec("talk", check_talk, "Speak through the robot speaker",
              needs={"talk_enabled": True}),
    CheckSpec("hear", check_hear, "Hear + transcribe a spoken phrase",
              needs={"allow_audio_in": True}),
    CheckSpec("autonomy", check_autonomy, "One full autonomous decision cycle"),
    CheckSpec("vslam", check_vslam, "Visual odometry / mapping"),
]
