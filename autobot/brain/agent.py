"""The agent: an event-driven, streaming brain (perceive continuously -> think on events -> act -> observe).

Instead of a rigid tick loop, the brain keeps a live PerceptionBuffer (latest frame, caption, telemetry,
recent speech) refreshed by background tasks, and a priority event queue. A Reasoner consumes events:
speech preempts idle wandering so it answers you immediately; motion is non-blocking so it can keep moving
while it thinks; a background captioner decouples vision latency from decisions. Every reaction also drives
an eye expression. It is provider-agnostic and fail-safe: any error stops the robot, never turns into motion.

The AI reaches the robot only through the shared RobotLink and the safety floor; capabilities are composed
from the skill registry and gated by per-capability user toggles (think/motion/video/audio-in/audio-out).
See docs/AI_BRAIN.md.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field as dfield
from typing import Awaitable, Callable

from ..config import Settings
from ..robot.link import RobotLink
from . import commands, locomotion, motion_model, reflex_vision, visual_motion
from .behavior import BehaviorController
from .reflex_vision import LoomingDetector
from . import navigator
from .curiosity import Curiosity
from .identity import Identity
from .memory import Memory
from .metrics import Metrics
from .motion_check import MotionConfirmer
from .omni_client import OmniClient, OmniError, omni_enabled
from .perception import Observation, perceive
from .tasks import TaskStore
from .vlm_client import (VlmClient, VlmError, brain_mode, hybrid_enabled, vlm_enabled,
                         vlm_perception_enabled)
from .providers import OpenAICompatibleClient, ProviderError
from .safety import SafetyFloor
from .skills import SkillContext, SkillRegistry, build_default_skills

EmitFn = Callable[[dict], Awaitable[None]]

MAX_TOOL_ROUNDS = 3      # model<->tool rounds allowed within a single reason cycle
HEARD_TTL = 15.0         # ignore heard speech older than this (s)
CAPTION_PERIOD = float(os.environ.get("AUTOBOT_CAPTION_SECONDS", "2.0"))  # background scene perception cadence (s)
PERCEIVE_PERIOD = 1.0    # how often the perceiver refreshes telemetry + frame (s)
EYE_COOLDOWN = 1.2       # min seconds between reflex eye changes (anti-spam)
# Non-LLM safety reflex: if the IR/ToF sensor reports an obstacle closer than this (cm), stop immediately
# (between cortex decisions) and arm a "turn" hint for the next decision. 0 disables. See docs/AI_BRAIN.md.
REFLEX_STOP_CM = float(os.environ.get("AUTOBOT_REFLEX_STOP_CM", "18"))
REFLEX_PERIOD = 0.4      # how often the reflex checks the latest cached telemetry (s)
# Hybrid roam: run the deterministic navigator for motion on most ticks; let the cortex (LLM) take every Nth
# roam tick for cognition/narration/memory. Keeps wandering fast+robust without starving higher reasoning.
CORTEX_EVERY = 4

# Event priorities (lower = handled first). Commands + speech + manual preempt autonomous wandering.
_PRIORITY = {"command": 0, "speech": 0, "manual": 0, "state": 1, "touch": 1, "idle": 2}

VISION_PROMPT = (
    "You are the eyes of a small two-wheeled roaming robot. In 1-3 concise sentences, describe this camera "
    "frame for navigation: where the open floor/clear paths are (left/center/right), obstacles or walls and "
    "roughly how close they are, any doorways or openings, people (and what they're doing), and notable "
    "objects. Be spatial and specific. Do not give advice — just describe what is visible.")


@dataclass(order=True)
class _Ev:
    priority: int
    seq: int
    kind: str = dfield(compare=False)
    data: dict = dfield(compare=False, default_factory=dict)


class PerceptionBuffer:
    """Continuously-updated snapshot of what the robot perceives, shared between the background sensors and
    the reasoner so a decision never has to block on a fresh perceive/caption."""

    def __init__(self):
        self.obs: Observation | None = None
        self.caption: str = ""
        self.caption_ts: float = 0.0
        self.frame_ts: float = 0.0
        self.telemetry: dict = {}
        self.transcripts: deque = deque(maxlen=8)


class AgentBrain:
    def __init__(self, settings: Settings, emit: EmitFn, link: RobotLink,
                 memory: Memory, identity: Identity):
        self.settings = settings
        self.emit = emit
        self.link = link
        self.memory = memory
        self.identity = identity
        self.safety = SafetyFloor()
        # Closed-loop motion confirmation: did the last AI move actually move us? (pose_provider wired by the
        # web server to VSLAM when available; falls back to camera frame-diff otherwise.)
        self.motion = MotionConfirmer()
        # Curiosity: tracks scene novelty + action repetition so the brain doesn't loop the same spot/move.
        self.curiosity = Curiosity()
        # Behavior: decides whether to roam at all (observe by default; roam only for a reason).
        self.behavior = BehaviorController()
        # Persistent scheduled tasks/reminders; the scheduler loop fires due ones as high-priority directives.
        self.tasks = TaskStore()
        # Movement calibration profile (controlled step/turn sizes); None until calibrated.
        from . import motion_profile as _mp
        self._mp = _mp
        self.motion_profile = _mp.load()
        self._motion_state: str | None = None      # last classified outcome (for status/UI)
        self._motion_reaction: object | None = None  # a pending stuck/blocked result the next decision reacts to
        self.skills = build_default_skills()
        self.ctx = SkillContext(link=link, settings=settings, safety=self.safety,
                                memory=memory, identity=identity, emit=emit)
        # Voice skill -> event queue: heard speech preempts idle wandering.
        self.ctx.on_speech = lambda text, speaker="voice": self.feed_speech(text, speaker, addressed=False)
        # Skills (e.g. recognition seeing a known face) can nudge the reasoner to act now.
        self.ctx.wake = self.wake
        self.ctx.tasks = self.tasks
        self.ctx.motion_profile = self.motion_profile
        self.ctx.behavior = self.behavior
        # VSLAM pose source — wired by the web server to SLAM.map_data when available. Powers spatial coverage
        # (curiosity) + place tagging/navigation; None => no spatial awareness (the brain still runs fine).
        self.pose_provider: Callable[[], dict] | None = None
        self.ctx.pose_provider = lambda: (self.pose_provider() if self.pose_provider else None)
        self.registry = SkillRegistry(self.skills, self.ctx)
        self.history: list[dict] = []
        self.status = "idle"           # idle | thinking | acting | error | paused | resting
        self.last_error: str | None = None
        self.last_tick_ts: float = 0.0
        self.last_observation: Observation | None = None
        self.buffer = PerceptionBuffer()
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._reason_lock = asyncio.Lock()
        self._events: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._speech_pending = asyncio.Event()
        self._last_eye = ("", 0.0)
        self._prev_state: tuple = ()
        self._was_touched = False
        self._idle_pending = False   # coalesce idle triggers so a slow reasoner can't accumulate a backlog
        self._reflex_active = False  # ToF reflex currently engaged (obstacle very close) — anti-spam latch
        self._reflex_blocked = False  # arm a one-shot "turn, don't go forward" hint for the next cortex decision
        self._looming = LoomingDetector()  # camera looming reflex (substitute for ToF on sensorless robots)
        self._loom_frame_ts = 0.0
        # Deterministic navigator (midbrain) state: roam-tick counter + anti-spin memory.
        self._nav_cycle = 0
        self._nav_last_dir = "right"
        self._nav_backed_up = False
        self._proprio_jpeg: bytes | None = None   # last frame, for the camera-based "am I moving" proprioception
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_spoken = ""       # last line we said aloud (for the SPEAK_UP "say that again" command)
        # Per-phase latency metrics (perceive/provider/tool/reason/...). See docs/MATURITY.md §2.
        self.metrics = Metrics()
        # Hybrid 'eyes' (VLM service) health: None=unknown, True=reachable, False=down (cortex falls back to
        # seeing the camera directly). Tracked from perceive outcomes + an explicit cold-start probe.
        self._vlm_ok: bool | None = None
        self._vlm_warned = False

    # --- hybrid vision ---
    def _hybrid(self, s: Settings) -> bool:
        return bool(s.ai_vision_model and s.ai_vision_model != s.ai_model)

    async def _caption(self, obs: Observation, s: Settings) -> str:
        url = obs.image_data_url()
        if not url:
            return ""
        vp = OpenAICompatibleClient(s.ai_base_url, s.ai_api_key, s.ai_vision_model, timeout=45.0)
        msg = [{"role": "user", "content": [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": url}},
        ]}]
        try:
            with self.metrics.timer("caption"):
                res = await vp.chat(msg, temperature=0.2)
            return (res.content or "").strip()
        except ProviderError as e:
            return f"(vision unavailable: {e})"

    async def _vlm_perceive(self, obs: Observation, s: Settings) -> str:
        """Hybrid brain: ask the VLM service (the robot's eyes) for a concise scene description that the
        tool-calling cortex reads. The VLM never decides a move here. Fail-soft -> empty string. Tracks the
        service's health so the cortex can fall back to seeing the camera directly when it's down."""
        if not obs.jpeg:
            return ""
        client = VlmClient()
        frames = [base64.b64encode(obs.jpeg).decode()]
        try:
            with self.metrics.timer("vlm_perceive"):
                res = await client.perceive(frames_b64=frames, robot_name=s.robot_name, persona=s.persona)
            await self._record_vlm_health(True)
            return (res.get("text") or "").strip()
        except VlmError:
            await self._record_vlm_health(False)
            return ""

    # --- hybrid 'eyes' health + fail-soft fallback (docs/MATURITY.md §1) ---
    async def _record_vlm_health(self, ok: bool) -> None:
        """Update the cached VLM-eyes health and announce transitions once (so the log doesn't spam)."""
        prev = self._vlm_ok
        self._vlm_ok = ok
        if ok and prev is False:
            self._vlm_warned = False
            await self.emit({"type": "thought",
                             "text": "(eyes: VLM service back online — hybrid perception restored)",
                             "ts": time.time()})
        elif (not ok) and not self._vlm_warned:
            self._vlm_warned = True
            await self.emit({"type": "thought",
                             "text": "(eyes: VLM service unreachable — cortex is using the camera directly)",
                             "ts": time.time()})

    async def _vlm_health(self, s: Settings) -> bool:
        """Explicit /health probe of the hybrid VLM eyes (used at cold start). False when not in hybrid."""
        if not hybrid_enabled(s):
            return False
        ok = await VlmClient().healthy()
        await self._record_vlm_health(ok)
        return ok

    def _vlm_perception_active(self, s: Settings) -> bool:
        """Hybrid eyes are the perception tier AND reachable. When the service is down (_vlm_ok is False) we
        fall back to single-model perception so the cortex isn't blind. Unknown (None) is treated as active;
        a cold-start probe / the next perceive corrects it."""
        return vlm_perception_enabled(s) and self._vlm_ok is not False

    # --- event posting / external inputs ---
    def _post(self, kind: str, data: dict | None = None) -> None:
        try:
            self._events.put_nowait(_Ev(_PRIORITY.get(kind, 2), next(self._seq), kind, data or {}))
        except Exception:  # noqa: BLE001
            pass

    def wake(self) -> None:
        """Nudge the reasoner to act now (an idle trigger). Kept for back-compat with callers."""
        self._post("idle")

    def reload_motion_profile(self) -> None:
        """Re-read data/motion_profile.json after a calibration run, and share it with the skills."""
        self.motion_profile = self._mp.load()
        self.ctx.motion_profile = self.motion_profile

    def _calib_drive(self, action: str) -> tuple[float, float, float] | None:
        """A drive vector for a coarse action, sized from the calibration profile when available so the robot
        takes controlled steps; falls back to the fixed _OMNI_DRIVE tuples otherwise."""
        p = self.motion_profile
        if p is not None:
            if action in ("forward",):
                return (p.forward_speed, 0.0, p.forward_duration)
            if action in ("back", "backward"):
                return (-p.forward_speed, 0.0, min(p.forward_duration, 0.8))
            if action == "left":
                return (0.0, -p.turn_rx, p.turn_duration)
            if action == "right":
                return (0.0, p.turn_rx, p.turn_duration)
        return self._OMNI_DRIVE.get(action)

    def feed_task(self, text: str) -> None:
        """A scheduled task fired: inject its text as a high-priority directive so the robot acts on it
        through the normal reasoning+safety path (addressed=True bypasses the name gate)."""
        self.feed_speech(text, speaker="your schedule", addressed=True)

    def feed_speech(self, text: str, speaker: str = "someone", addressed: bool = False) -> None:
        """Inject heard/typed speech as a high-priority event so the robot answers promptly (preempting any
        idle wandering). Respects the audio-in toggle for spoken (non-addressed) input."""
        text = (text or "").strip()
        if not text:
            return
        if self.settings.snapshot().asleep:
            return   # go-dark: ignore all heard/typed speech until woken
        if not addressed:
            from . import audio_state
            if audio_state.is_speaking():
                return   # echo gate: don't react to our own TTS bleeding into the mic
        self.behavior.note_activity()   # someone interacted -> reset the idle-patrol timer
        self.ctx.heard.clear()
        self.ctx.heard.update({"text": text, "ts": time.time(), "speaker": speaker, "addressed": addressed})
        self.buffer.transcripts.append({"text": text, "speaker": speaker, "ts": time.time()})
        # Always-respected voice commands: a fast keyword match that preempts normal reasoning. Owner-gated
        # ones need the owner (or a dashboard/addressed message) when owner-only obedience is on.
        intent = commands.match(text)
        if intent:
            s = self.settings.snapshot()
            allowed = (intent in commands.ALWAYS) or addressed or self.identity.authority_active(s)
            if allowed:
                if intent == "STOP" and self._loop is not None:   # instant halt, even mid-think
                    try:
                        asyncio.run_coroutine_threadsafe(self._safe_stop(), self._loop)
                    except Exception:  # noqa: BLE001
                        pass
                self._post("command", {"intent": intent, "text": text})
                return
        self._speech_pending.set()
        self._post("speech", {"addressed": addressed})

    # --- lifecycle ---
    def start(self):
        if self._running:
            return
        self._running = True
        try:
            self._loop = asyncio.get_event_loop()   # for thread-safe scheduling from STT callbacks
        except RuntimeError:
            self._loop = None
        self.registry.start_background()
        self._tasks = [
            asyncio.create_task(self._perceiver_loop()),
            asyncio.create_task(self._captioner_loop()),
            asyncio.create_task(self._reflex_loop()),
            asyncio.create_task(self._scheduler_loop()),
            asyncio.create_task(self._idle_loop()),
            asyncio.create_task(self._reasoner_loop()),
        ]

    async def stop_loop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []

    def _brain_ready(self, s: Settings) -> tuple[bool, str]:
        if not s.setup_complete:
            return False, "setup not complete — finish the first-run wizard"
        # The modular vision brain (light VLM) or the omni brain replace the OpenAI-compatible LLM, so when
        # either is configured we don't need an ai_model/endpoint.
        if vlm_enabled(s) or omni_enabled(s):
            return True, ""
        if not s.ai_model or not s.ai_base_url:
            return False, "no AI model/endpoint configured — pick a brain in setup"
        local = any(h in s.ai_base_url for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))
        if not s.ai_api_key and not local:
            return False, "no AI API key — add one in setup (or use a local endpoint)"
        return True, ""

    # --- background sensors ---
    async def _perceiver_loop(self):
        """Keep the buffer's frame + telemetry fresh, and post STATE events when the robot's availability
        changes (connected / sleeping / charging) so the reasoner reacts to docking/charging immediately."""
        while self._running:
            try:
                s = self.settings.snapshot()
                ready, _ = self._brain_ready(s)
                if ready and not s.asleep:
                    obs = await perceive(self.link, want_image=s.allow_video)
                    self.buffer.obs = obs
                    self.buffer.telemetry = obs.telemetry
                    self.last_observation = obs
                    if obs.has_image:
                        self.buffer.frame_ts = obs.ts
                        # Proprioception substitute: how much the view changed since the last frame -> a cheap
                        # "am I moving?" cue (the Air 2 has no IMU/odometry). Advisory; surfaced in telemetry.
                        if getattr(obs, "jpeg", None):
                            prev = self._proprio_jpeg
                            self._proprio_jpeg = obs.jpeg
                            if prev is not None:
                                diff = visual_motion.frame_diff(prev, obs.jpeg)
                                if diff is not None:
                                    m = motion_model.for_variant(getattr(s, "robot_variant", "AIR2"))
                                    obs.telemetry["self_motion"] = round(diff, 4)
                                    obs.telemetry["moving"] = bool(diff >= m.move_diff)
                    t = obs.telemetry
                    state = (bool(t.get("connected")), bool(t.get("awake", True)),
                             bool(t.get("resting")), int(t.get("charge", 0) or 0))
                    if state != self._prev_state:
                        self._prev_state = state
                        self._post("state")
                    # Touch/bump (IMU) -> instant surprised eyes + a high-priority reaction.
                    if t.get("touched") and not self._was_touched:
                        self._was_touched = True
                        self.behavior.note_activity()
                        await self._express("surprised")
                        self._post("touch")
                    elif not t.get("touched"):
                        self._was_touched = False
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(PERCEIVE_PERIOD)

    async def _captioner_loop(self):
        """Continuously caption the newest frame (hybrid vision) so decisions read a ready description instead
        of blocking on the vision model each time."""
        while self._running:
            try:
                s = self.settings.snapshot()
                obs = self.buffer.obs
                fresh = (not s.asleep and obs is not None and obs.has_image and s.allow_video
                         and self.buffer.frame_ts > self.buffer.caption_ts)
                if hybrid_enabled(s):
                    # Hybrid brain: the VLM is the eyes — perceive the scene for the cortex to reason over.
                    # If the eyes service is unknown/down, probe it so the cortex's fallback decision is fresh.
                    if self._vlm_ok is None:
                        await self._vlm_health(s)
                    if fresh:
                        cap = await self._vlm_perceive(obs, s)
                        if cap:
                            await self._set_caption(cap)
                elif vlm_enabled(s) or omni_enabled(s):
                    pass  # the vision brain sees the frame itself each cycle — no separate caption model
                elif self._hybrid(s) and fresh:
                    cap = await self._caption(obs, s)
                    if cap:
                        await self._set_caption(cap)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(CAPTION_PERIOD)

    def _current_pose(self) -> dict | None:
        """The live VSLAM map_data dict, or None if SLAM isn't running/enabled. Fail-soft."""
        try:
            p = self.pose_provider() if self.pose_provider else None
            if isinstance(p, dict) and p.get("enabled") and p.get("pose"):
                return p
        except Exception:  # noqa: BLE001
            pass
        return None

    def _apply_pose(self, obs: Observation) -> None:
        """Attach the rough VSLAM pose to the observation and update the spatial-coverage signal so the brain
        senses where it's already been. Pose is advisory (monocular, up-to-scale) — never trusted as truth."""
        pose = self._current_pose()
        if not pose:
            return
        pp = pose.get("pose") or {}
        obs.telemetry["pose"] = pp
        try:
            self.curiosity.note_position(float(pp.get("x", 0.0)), float(pp.get("y", 0.0)),
                                         float(pp.get("yaw_deg", 0.0)))
        except Exception:  # noqa: BLE001
            pass

    async def _set_caption(self, cap: str) -> None:
        """Store a fresh scene description and update the curiosity (novelty/boredom) signal from it."""
        self.buffer.caption = cap
        self.buffer.caption_ts = time.time()
        try:
            self.curiosity.note_scene(cap)
        except Exception:  # noqa: BLE001
            pass
        await self.emit({"type": "thought", "text": "(sees) " + cap, "ts": time.time()})

    async def _reflex_loop(self):
        """A tiny, non-LLM safety reflex: between cortex decisions, watch the cached ToF/IR distance and stop
        the robot the instant something is very close ahead, then arm a one-shot 'turn, don't push forward'
        hint for the next decision. Cheap (reads the latest buffered telemetry; never calls the model)."""
        while self._running:
            try:
                s = self.settings.snapshot()
                if s.allow_motion and not s.asleep:
                    t = self.buffer.telemetry or {}
                    resting = bool(t.get("resting"))
                    tof = t.get("tof", t.get("distance"))
                    have_tof = isinstance(tof, (int, float)) and tof >= 0
                    close = have_tof and REFLEX_STOP_CM > 0 and 0 <= tof < REFLEX_STOP_CM and not resting
                    reason = f"obstacle {int(tof)}cm ahead" if close else ""
                    # Camera looming reflex: only when there's no real ToF (e.g. Air 2). Diverging optical flow
                    # in the center = approaching something head-on; ignores turns (no divergence on a pivot).
                    if not have_tof and reflex_vision.LOOM_THRESHOLD > 0 and not resting:
                        obs = self.buffer.obs
                        if obs is not None and getattr(obs, "jpeg", None) and self.buffer.frame_ts > self._loom_frame_ts:
                            self._loom_frame_ts = self.buffer.frame_ts
                            score = self._looming.update(obs.jpeg)
                            if score >= reflex_vision.LOOM_THRESHOLD:
                                close = True
                                reason = "something looming close ahead"
                    if close and not self._reflex_active:
                        self._reflex_active = True
                        self._reflex_blocked = True   # next cortex decision should turn, not drive forward
                        with self.metrics.timer("reflex_stop"):
                            await self._safe_stop()
                        await self._express("surprised")
                        await self.emit({"type": "thought",
                                         "text": f"(reflex: {reason} — stopping)", "ts": time.time()})
                    elif not close:
                        self._reflex_active = False
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(REFLEX_PERIOD)

    async def _scheduler_loop(self):
        """Fire due scheduled tasks/reminders as high-priority directives. Runs on the loop thread so posting
        events is safe. Skipped while asleep; firing still respects the safety floor for any motion."""
        while self._running:
            try:
                s = self.settings.snapshot()
                if not s.asleep:
                    for t in self.tasks.due():
                        await self.emit({"type": "task_fired", "id": t.id, "text": t.text, "ts": time.time()})
                        self.memory.log_event(f"scheduled task fired: {t.text}", source="system")
                        self.feed_task(t.text)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(5.0)

    async def _idle_loop(self):
        """Drive autonomous wandering on a cadence when nothing else is happening."""
        while self._running:
            s = self.settings.snapshot()
            ready, _ = self._brain_ready(s)
            if ready and not s.asleep and s.autonomy == "auto" and s.allow_think and not self._idle_pending:
                # Pre-flight gate: don't wander autonomously until movement is calibrated (manual still works).
                if getattr(s, "require_calibration", True) and self.motion_profile is None:
                    if self.status != "needs_calibration":
                        await self._set_status("needs_calibration",
                                               "calibrate movement before autonomous roaming")
                else:
                    self._idle_pending = True
                    self._post("idle")
            await asyncio.sleep(max(1.0, s.tick_seconds))

    async def _reasoner_loop(self):
        while self._running:
            try:
                ev = await self._events.get()
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                break
            s = self.settings.snapshot()
            ready, why = self._brain_ready(s)
            if not ready:
                if self.status not in ("idle", "paused", "error"):
                    await self._set_status("paused", f"brain not configured — {why}")
                continue
            if s.asleep:
                if self.status != "asleep":
                    await self._safe_stop()
                    await self._set_status("asleep", "FreeBo is asleep")
                continue
            # Gating per trigger.
            if ev.kind == "idle":
                self._idle_pending = False
                if not (s.autonomy == "auto" and s.allow_think):
                    continue
            if ev.kind == "speech":
                self._speech_pending.clear()
                if not ev.data.get("addressed") and not s.allow_audio_in:
                    continue
            if ev.kind in ("state", "touch") and not s.allow_think:
                continue
            try:
                if ev.kind == "command":
                    await self._apply_command(ev.data.get("intent", ""), ev.data.get("text", ""), s)
                else:
                    await self._reason(ev.kind, s)
            except Exception as e:  # noqa: BLE001
                await self._set_status("error", f"{type(e).__name__}: {e}")
                await self._safe_stop()

    # --- prompt ---
    def build_system_prompt(self, s: Settings) -> str:
        talk = "ENABLED — you may use `say`." if s.talk_enabled else "DISABLED — do not try to speak."
        name = s.robot_name or "Autobot"
        sections = [
            f"You are {name}, a small two-wheeled Enabot robot. You see through its camera, hear through its "
            f"mic, drive its two wheels, control its expressive eyes, and (optionally) speak.",
            f"PERSONA:\n{s.persona}",
            f"YOUR GOAL:\n{s.goal}",
        ]
        ident = self.identity.summary_for_prompt(s)
        if ident:
            sections.append("WHO YOU'RE WITH:\n" + ident)
        mem = self.memory.summary_for_prompt()
        if mem:
            sections.append(mem)
        skill_bits = self.registry.system_prompt_additions()
        if skill_bits:
            sections.append("YOUR SKILLS:\n" + skill_bits)
        cur = self.curiosity.prompt_fragment()
        if cur and self.behavior.current.scope == "roam":
            sections.append(cur)
        sections.append(self._mode_prompt(s))
        sections.append(self.behavior.prompt_block(name))
        sections.append(motion_model.guidance_text(s.robot_variant))
        sections.append(
            "HOW YOU ACT:\n"
            "- You run in real time: you may be triggered by a fresh look, by something you hear, or by a "
            "change in your state. When someone speaks to you, answering them is the priority.\n"
            "- YOUR EYES: each turn you get a 'WHAT YOUR EYES SEE' block — a fresh description of your camera "
            "view (scene, objects, people, and which directions have open floor vs. close obstacles). Treat it "
            "as your own sight and act on it: steer toward the open paths it names, toward people and "
            "interesting objects, and away from anything it says is close ahead.\n"
            "- A fast reflex will stop you if something gets too close; if you're told a reflex stopped you, "
            "turn to a clear direction rather than pushing forward.\n"
            "- EXPRESS YOURSELF: set your eyes to match the moment on most actions (curious while exploring, "
            "happy/excited when you see a person or something new, surprised when blocked, love when greeted, "
            "sleepy when resting). Use `set_eyes` freely.\n"
            "- BE CHATTY LIKE A CURIOUS PET (only if talk is enabled): comment out loud on NEW or interesting "
            "things, greet people by name, and react — but don't narrate every single step or repeat yourself.\n"
            "- ACT BY CALLING TOOLS (function calls) — to speak you MUST call the `say` tool, to move call "
            "`drive`, etc. Do NOT just write what you would do as text; the body only responds to real calls.\n"
            f"- Talk is {talk}\n"
            "- If you have no camera frame, don't drive — `wait` and look instead.\n"
            "- A low-level motor controller turns your drive intents into safe, confirmed moves — give SIMPLE "
            "intents (a direction) and re-check your eyes after each; don't micro-manage speed. If your status "
            "says you were JUST TOUCHED/BUMPED, react like a pet would — show surprise, turn to look at who "
            "did it, and greet/respond.\n"
            "- BUILD UNDERSTANDING: use `save_place` to map distinct spots and `remember` notable things (and "
            "WHERE you saw them) so you can find them and describe the space later.\n"
            "- POWER: if your status shows you are charging/docked/resting, do NOT try to drive — stay put, "
            "keep your eyes sleepy, and just observe or chat until you're free again. If battery is low, `dock`.")
        sections.append(
            "SAFETY (also enforced by the system):\n"
            "- Speed is clamped to the user's limit. You cannot change the user's settings or capability "
            "toggles. Don't try.")
        return "\n\n".join(sections)

    # Motion tools that physically move the robot across the floor (excluded from the toolset when motion is
    # off / resting, so the model can't keep trying to drive and failing).
    _MOTION_TOOLS = {"drive", "dock", "undock"}

    # Battery at/above this while charging/docked => the robot is free to leave the dock and roam. A fully
    # charged robot shouldn't be pinned in place by a charging/resting flag (the Air 2 also keeps reporting
    # charge=1 briefly after it rolls off the contacts). See _resting().
    LEAVE_DOCK_BATTERY = 90

    def _resting(self, telemetry: dict) -> bool:
        """Whether the robot should be treated as 'parked, don't drive'. True when charging/docked UNLESS the
        battery is topped up (>LEAVE_DOCK_BATTERY%), in which case it's allowed to head out."""
        charging = bool(telemetry.get("resting")) or (telemetry.get("charge") == 1)
        if not charging:
            return False
        batt = telemetry.get("battery", -1)
        if isinstance(batt, (int, float)) and batt > self.LEAVE_DOCK_BATTERY:
            return False
        return True

    def _motion_allowed(self, s: Settings, resting: bool) -> bool:
        return bool(s.allow_motion) and s.autonomy in ("assist", "auto") and not resting

    def _tool_exclusions(self, s: Settings, resting: bool) -> set[str]:
        if not self._motion_allowed(s, resting):
            return set(self._MOTION_TOOLS)
        if s.mode == "conversational":
            # may rotate in place but not roam — drop dock/undock (those drive across the room)
            return {"dock", "undock"}
        return set()

    def _mode_prompt(self, s: Settings) -> str:
        mode = getattr(s, "mode", "explore")
        if mode == "command":
            directive = (s.directive or "").strip() or "(no directive set — wait for one and just observe)"
            return ("YOUR CURRENT MODE: COMMAND.\n"
                    f"Your single active directive is: \"{directive}\".\n"
                    "- Pursue this directive directly and persistently. If it names a target (a person, a pet, "
                    "an object), use your camera to FIND it: scan by turning, then once you see it, DRIVE toward "
                    "it and keep following/tracking it as it moves. Keep it roughly centered in view.\n"
                    "- Re-check the camera often and correct course. If you lose the target, turn to search, "
                    "then resume. Narrate what you're doing briefly.\n"
                    "- Onboard obstacle avoidance is ON, so move confidently. Stop when the directive is "
                    "satisfied or no longer makes sense, and say so.")
        if mode == "conversational":
            return ("YOUR CURRENT MODE: CONVERSATIONAL.\n"
                    "- Stay where you are. You may ONLY rotate in place (turn left/right) to keep the person "
                    "you're talking with centered in your camera — do NOT drive forward or roam (the system "
                    "enforces this).\n"
                    "- Focus on listening and replying naturally. Use your eyes and (if enabled) voice to be "
                    "engaging. Track the speaker with small turns; otherwise hold still.")
        return ("YOUR CURRENT MODE: COMPANION (home).\n"
                "- You are a calm, curious companion living in the home. MOST of the time you stay put and "
                "watch, turning in place to look around and commenting on what's new or interesting. You do "
                "NOT roam constantly.\n"
                "- The 'RIGHT NOW' line below tells you exactly what to do this moment (observe, go greet "
                "someone, patrol, etc.). Follow it. When it says observe, do not drive across the room.\n"
                "- When you DO move, steer using WHAT YOUR EYES SEE: aim for the open paths it reports and turn "
                "away from anything close ahead. You have NO physical bumper — if a reflex stops you, turn to a "
                "clear direction instead of pushing forward. Name and remember notable things/places.")

    def _heard_line(self, s: Settings) -> str | None:
        heard = self.ctx.heard
        if not heard or (time.time() - heard.get("ts", 0)) > HEARD_TTL:
            return None
        text = str(heard.get("text", "")).strip()
        if not text:
            return None
        addressed = bool(heard.get("addressed"))
        if not addressed and s.require_name and (s.robot_name or "").lower() not in text.lower():
            return None
        who = heard.get("speaker", "someone")
        return f'You just heard {who} say: "{text}". Respond if appropriate.'

    # --- closed-loop motion confirmation ---
    async def _confirm_motion(self, s: Settings, obs: Observation) -> None:
        """If a move was issued last cycle, verify the robot actually moved (camera frame-diff + VSLAM pose).
        Surfaces a `motion` event for the UI, records the state for status, and arms a one-shot reaction so the
        next decision can escape a stuck/blocked situation instead of pushing forward into a wall again."""
        self._motion_reaction = None   # one-shot: only valid for the cycle right after a move
        if not getattr(s, "confirm_motion", True) or not self.motion.has_pending():
            return
        res = self.motion.confirm(obs.jpeg)
        if not res:
            return
        self._motion_state = res.state
        await self.emit({"type": "motion", "state": res.state, "detail": res.detail,
                         "evidence": res.evidence, "ts": time.time()})
        if res.state in ("stuck", "blocked"):
            self._motion_reaction = res
            await self.emit({"type": "thought", "text": f"(motion check: {res.state} — {res.detail})",
                             "ts": time.time()})

    def _stuck_forward(self, action: str) -> bool:
        r = self._motion_reaction
        return (r is not None and getattr(r, "state", "") in ("stuck", "blocked")
                and action in ("forward", "back", "backward"))

    # --- robustness: small local models (e.g. qwen2.5:7b via Ollama) often write tool calls as prose
    #     (`say("hi")`, `drive(direction="right", duration=1.0)`) instead of structured tool_calls, and answer
    #     speech as plain chat. These helpers recover real tool calls / speech so actions actually execute. ---
    @staticmethod
    def _split_top_commas(s: str) -> list[str]:
        parts: list[str] = []
        buf: list[str] = []
        quote = None
        depth = 0
        for ch in s:
            if quote:
                buf.append(ch)
                if ch == quote:
                    quote = None
                continue
            if ch in "\"'":
                quote = ch
                buf.append(ch)
                continue
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        if buf:
            parts.append("".join(buf))
        return parts

    @staticmethod
    def _coerce_arg(v: str):
        v = v.strip()
        if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
            return v[1:-1]
        low = v.lower()
        if low in ("true", "false"):
            return low == "true"
        if low in ("none", "null"):
            return None
        for cast in (int, float):
            try:
                return cast(v)
            except ValueError:
                continue
        return v

    @staticmethod
    def _json_tool_calls(content: str, names: set[str]) -> list[dict]:
        """Recover tool calls a model dumped as JSON objects in its text, e.g.
        `{"name":"say","arguments":{"text":"hi"}}` (qwen/Ollama do this when the tool template misses)."""
        import json as _json
        calls: list[dict] = []
        n = len(content)
        i = 0
        while i < n:
            if content[i] != "{":
                i += 1
                continue
            depth = 0
            quote = None
            j = i
            while j < n:
                ch = content[j]
                if quote:
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == quote:
                        quote = None
                elif ch == '"':
                    quote = ch
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            blob = content[i:j]
            obj = None
            try:
                obj = _json.loads(blob)
            except Exception:  # noqa: BLE001
                pass
            if isinstance(obj, dict) and obj.get("name") in names:
                args = obj.get("arguments", obj.get("parameters", {}))
                if isinstance(args, str):
                    try:
                        args = _json.loads(args)
                    except Exception:  # noqa: BLE001
                        args = {}
                calls.append({"id": f"call_j{len(calls)}", "name": obj["name"],
                              "arguments": args if isinstance(args, dict) else {}})
            i = j if j > i else i + 1
        return calls

    def _prose_tool_calls(self, content: str, tools: list[dict]) -> list[dict]:
        """Recover tool calls a small model emitted as TEXT instead of structured calls — JSON objects first,
        then python-style `name(args)` prose. KNOWN tool names only."""
        import re as _re
        if not content:
            return []
        meta: dict[str, list[str]] = {}
        for t in tools:
            fn = t.get("function", {})
            nm = fn.get("name")
            if not nm:
                continue
            params = fn.get("parameters", {}) or {}
            meta[nm] = params.get("required") or list((params.get("properties") or {}).keys())
        text = content.replace("<tool_call>", " ").replace("</tool_call>", " ")
        json_calls = self._json_tool_calls(text, set(meta))
        if json_calls:
            return json_calls
        calls: list[dict] = []
        n = len(text)
        for m in _re.finditer(r"([A-Za-z_]\w*)\s*\(", text):
            nm = m.group(1)
            if nm not in meta:
                continue
            j = m.end()
            depth = 1
            quote = None
            while j < n:
                ch = text[j]
                if quote:
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == quote:
                        quote = None
                elif ch in "\"'":
                    quote = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            args: dict = {}
            for part in self._split_top_commas(text[m.end():j]):
                part = part.strip()
                if not part:
                    continue
                kw = _re.match(r"^([A-Za-z_]\w*)\s*[=:]\s*(.+)$", part, _re.S)
                if kw:
                    args[kw.group(1)] = self._coerce_arg(kw.group(2))
                elif meta.get(nm):
                    args.setdefault(meta[nm][0], self._coerce_arg(part))
            calls.append({"id": f"call_txt{len(calls)}", "name": nm, "arguments": args})
        return calls

    @staticmethod
    def _clean_reasoning(content: str) -> str:
        """Strip tool-call syntax (JSON objects, name(...) calls, <tool_call> tags, code fences) from content,
        leaving only the natural-language reasoning — for a readable thought log and safe speech."""
        import re as _re
        text = content or ""
        text = text.replace("<tool_call>", " ").replace("</tool_call>", " ")
        text = _re.sub(r"```[\s\S]*?```", " ", text)         # code fences
        text = _re.sub(r"\{[^{}]*\}", " ", text)               # flat JSON objects (covers most tool dumps)
        text = _re.sub(r"\{[\s\S]*?\}", " ", text)             # any leftover braces blocks
        text = _re.sub(r"\b[A-Za-z_]\w*\s*\([^)]*\)", " ", text)  # name(args) prose calls
        text = _re.sub(r"[ \t]+", " ", text)
        return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip()).strip()

    def _spoken_from_content(self, content: str) -> str:
        """Natural-language sentence to speak for a pure chat reply. Returns '' if anything tool/JSON-ish
        remains, so the robot NEVER vocalizes raw tool syntax."""
        if not content or "{" in content or "}" in content or "<tool_call" in content:
            # contained tool-call syntax — those become real calls; don't also speak the residue.
            cleaned = self._clean_reasoning(content)
        else:
            cleaned = content.strip()
        if "{" in cleaned or "}" in cleaned or '"name"' in cleaned or "arguments" in cleaned:
            return ""
        cleaned = cleaned.strip()
        return cleaned[:300] if len(cleaned) >= 3 else ""

    @staticmethod
    def _coarse_dir(ly: float, rx: float) -> str:
        """Map a clamped drive vector to a coarse label for the curiosity repetition signal."""
        if abs(rx) > abs(ly):
            return "right" if rx > 0 else "left"
        if ly > 0:
            return "forward"
        if ly < 0:
            return "back"
        return "turn"

    # --- expression reflex (eyes without an LLM round) ---
    async def _express(self, state: str):
        now = time.time()
        if self._last_eye[0] == state and (now - self._last_eye[1]) < EYE_COOLDOWN:
            return
        self._last_eye = (state, now)
        try:
            await self.link.action(f"eyes_{state}")
            await self.emit({"type": "thought", "text": f"(eyes: {state})", "ts": now})
        except Exception:  # noqa: BLE001
            pass

    def _reflex_for(self, kind: str, obs: Observation | None) -> str:
        cap = (self.buffer.caption or "").lower()
        if kind == "touch":
            return "surprised"
        if kind == "speech":
            return "happy"
        if obs is not None and self._resting(obs.telemetry):
            return "sleepy"
        if "person" in cap or "someone" in cap or "people" in cap:
            return "curious"
        if "wall" in cap and ("close" in cap or "front" in cap):
            return "surprised"
        return "curious"

    # --- one reason cycle ---
    async def tick(self, force: bool = False) -> dict:
        """Run a single reason cycle now (manual trigger from /api/tick)."""
        s = self.settings.snapshot()
        if s.asleep:
            return {"ok": False, "error": "asleep"}
        ready, why = self._brain_ready(s)
        if not ready:
            await self._set_status("paused", f"brain not configured — {why}")
            return {"ok": False, "error": f"brain not configured — {why}"}
        return await self._reason("manual", s)

    async def _reason(self, trigger: str, s: Settings) -> dict:
        """Time one full reason cycle (incl. lock wait) and delegate to the real loop. See docs/MATURITY.md §2."""
        t0 = time.perf_counter()
        try:
            return await self._reason_inner(trigger, s)
        finally:
            self.metrics.record("reason", (time.perf_counter() - t0) * 1000.0)

    async def _reason_inner(self, trigger: str, s: Settings) -> dict:
        async with self._reason_lock:
            self.safety.begin_tick()
            self.ctx.flags.clear()
            self.last_tick_ts = time.time()

            with self.metrics.timer("perceive"):
                obs = self.buffer.obs or await perceive(self.link, want_image=s.allow_video)
                self.last_observation = obs
                self._apply_pose(obs)
                await self.registry.on_observe(obs)
                await self._confirm_motion(s, obs)
            eye_anims = obs.telemetry.get("eye_animations", [])
            await self.emit({"type": "observation", "summary": obs.text_summary(),
                             "telemetry": obs.telemetry, "ts": obs.ts})

            resting = self._resting(obs.telemetry)

            # Decide the movement behavior for this cycle and hand the scope to the safety floor (hard gate).
            beh = self.behavior.decide(s, resting=resting, present_people=self.identity.present_people(),
                                       owner_name=s.owner_name)
            self.safety.set_scope(beh.scope)

            # Blind: no camera and not connected -> stop and idle (don't burn the model).
            if not obs.has_image and not obs.telemetry.get("connected", True):
                await self._safe_stop()
                await self._set_status("idle", "no camera/robot — waiting")
                return {"ok": True, "skipped": "no_camera"}

            # Resting (charging / docked / asleep): respect it. Don't drive on autonomous triggers; only a
            # direct speech still gets a (no-motion) reply.
            if resting and trigger in ("idle", "state"):
                await self._safe_stop()
                await self._express("sleepy")
                await self._set_status("resting", "charging/docked — staying put")
                return {"ok": True, "skipped": "resting"}

            await self._set_status("thinking")
            # Instant expression so the eyes react even before the model replies.
            await self._express(self._reflex_for(trigger, obs))

            # The modular vision brain: a light VLM sees the frame + decides a move; hearing (Whisper) and
            # speech (Piper) are handled here in the app. No monolithic model, no tool-calling LLM.
            if vlm_enabled(s):
                try:
                    res = await self._reason_vlm(trigger, s, obs, resting)
                    self.last_error = None
                    await self._set_status("idle")
                    return res
                except VlmError as e:
                    await self._set_status("error", f"vision: {e}")
                    await self._safe_stop()
                    return {"ok": False, "error": str(e)}

            # The omni brain (MiniCPM-o) path: it sees the frame, (optionally) hears, decides a move, and
            # speaks — all in one model call. No tool-calling LLM.
            if omni_enabled(s):
                try:
                    res = await self._reason_omni(trigger, s, obs, resting)
                    self.last_error = None
                    await self._set_status("idle")
                    return res
                except OmniError as e:
                    await self._set_status("error", f"omni: {e}")
                    await self._safe_stop()
                    return {"ok": False, "error": str(e)}

            # Hybrid cortex path: make sure we know whether the VLM 'eyes' are up before deciding whether the
            # cortex reads a caption (text-only) or falls back to the camera frame directly.
            if hybrid_enabled(s) and self._vlm_ok is None:
                await self._vlm_health(s)

            # Midbrain: routine wandering is decided by the deterministic navigator (fast CPU, no LLM round),
            # so the robot reliably drives toward open space / backs off obstacles instead of waiting on the
            # cortex to format a move. The cortex still owns conversation/goals (any heard/directive trigger)
            # and takes every CORTEX_EVERY-th roam tick for cognition + narration + memory.
            roam_motion = (hybrid_enabled(s) and trigger in ("idle", "state") and beh.scope == "roam"
                           and s.allow_motion and not resting and not self._heard_line(s)
                           and not (s.directive or "").strip())
            if roam_motion:
                self._nav_cycle += 1
                if self._nav_cycle % CORTEX_EVERY != 0:
                    return await self._navigate_roam(trigger, s, obs)

            provider = OpenAICompatibleClient(s.ai_base_url, s.ai_api_key, s.ai_model)
            tools = self.registry.schemas(eye_anims, exclude=self._tool_exclusions(s, resting))
            system = self.build_system_prompt(s)
            user_msg = self._observation_message(obs, s, resting=resting)
            messages = [{"role": "system", "content": system}] + self._trimmed_history() + [user_msg]
            self._append_history({"role": "user", "content": obs.text_summary()})

            actions: list[dict] = []
            try:
                for _round in range(MAX_TOOL_ROUNDS):
                    with self.metrics.timer("provider"):
                        result = await provider.chat(messages, tools=tools)
                    # Recover tool calls from prose if the model didn't emit structured ones (small local
                    # models often do), and speak a plain-chat reply when answering speech.
                    tool_calls = result.tool_calls
                    from_text = False
                    if not tool_calls and result.content:
                        tool_calls = self._prose_tool_calls(result.content, tools)
                        from_text = bool(tool_calls)
                    if (not tool_calls and result.content and s.talk_enabled
                            and trigger in ("speech", "manual", "touch")):
                        spoken = self._spoken_from_content(result.content)
                        if spoken:
                            tool_calls = [{"id": "call_say", "name": "say", "arguments": {"text": spoken}}]
                            from_text = True
                    if result.content:
                        # Show/keep only the natural-language reasoning (never the raw tool JSON/prose).
                        display = self._clean_reasoning(result.content) if from_text else result.content
                        if display:
                            await self.emit({"type": "thought", "text": display, "ts": time.time()})
                            self._append_history({"role": "assistant", "content": display})
                    if not tool_calls:
                        break
                    assistant_tc = {
                        "role": "assistant", "content": "" if from_text else (result.content or ""),
                        "tool_calls": [{"id": tc["id"] or f"call_{i}", "type": "function",
                                        "function": {"name": tc["name"],
                                                     "arguments": json.dumps(tc["arguments"])}}
                                       for i, tc in enumerate(tool_calls)],
                    }
                    messages.append(assistant_tc)
                    await self._set_status("acting")
                    for i, tc in enumerate(tool_calls):
                        cid = tc["id"] or f"call_{i}"
                        await self.emit({"type": "tool_call", "name": tc["name"],
                                         "args": tc["arguments"], "ts": time.time()})
                        with self.metrics.timer("tool"):
                            res = await self.registry.execute(tc["name"], tc["arguments"])
                        if tc["name"] == "say" and isinstance(res, dict) and res.get("ok"):
                            self._last_spoken = str(tc["arguments"].get("text", "")) or self._last_spoken
                        actions.append({"name": tc["name"], "args": tc["arguments"], "result": res})
                        await self.emit({"type": "tool_result", "name": tc["name"], "result": res,
                                         "ts": time.time()})
                        # Arm closed-loop motion confirmation for the next cycle.
                        if (tc["name"] == "drive" and isinstance(res, dict) and res.get("ok")
                                and isinstance(res.get("drove"), dict)):
                            dly, drx = res["drove"].get("ly", 0.0), res["drove"].get("rx", 0.0)
                            self.motion.record(obs.jpeg, dly, drx)
                            self.curiosity.note_action(self._coarse_dir(dly, drx))
                        messages.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(res)})
                        self._append_history({"role": "tool", "tool_call_id": cid, "content": json.dumps(res)})
                    # Preempt low-priority wandering if someone just spoke — handle that next.
                    if self._speech_pending.is_set() and trigger != "speech":
                        break
                    if self.ctx.flags.pop("look", False):
                        # use the freshest buffered frame/caption rather than blocking on a new perceive
                        obs = self.buffer.obs or obs
                        self.last_observation = obs
                        messages.append(self._observation_message(obs, s, prefix="(fresh frame)"))
                self.last_error = None
                await self._set_status("idle")
            except ProviderError as e:
                await self._set_status("error", str(e))
                await self._safe_stop()
                return {"ok": False, "error": str(e)}

            return {"ok": True, "actions": actions, "observation": obs.text_summary()}

    # --- midbrain navigator (deterministic roam motion; no LLM round) ---
    async def _navigate_roam(self, trigger: str, s: Settings, obs: Observation) -> dict:
        """Pick + execute one wandering move from the VLM clearance + CPU reflexes, with no cortex call. CPU
        reflexes (looming / confirmed-blocked) override the caption so we back off / turn instead of crashing."""
        looming = self._reflex_blocked
        blocked = self._stuck_forward("forward") or looming
        clear = navigator.parse_clearance(obs.caption or self.buffer.caption)
        mv = navigator.choose(clear, blocked_ahead=blocked, looming=looming,
                              last_dir=self._nav_last_dir, backed_up_last=self._nav_backed_up)
        # Consume the one-shot reflex/stuck flags now that we've reacted to them.
        self._reflex_blocked = False
        self._motion_reaction = None
        action = mv.action
        self._nav_backed_up = (action == "back")
        if action in ("left", "right"):
            self._nav_last_dir = action

        await self.emit({"type": "thought", "text": f"(nav: {mv.reason})", "ts": time.time()})
        await self._express("surprised" if (looming or blocked or action == "back") else "curious")

        actions: list[dict] = []
        vec = self._calib_drive(action)
        if vec and self._motion_allowed(s, resting=False):
            ly, rx, _dur = vec
            await self._set_status("acting")
            await self.emit({"type": "tool_call", "name": "drive", "args": {"action": action},
                             "ts": time.time()})
            res = await locomotion.drive(link=self.link, safety=self.safety, settings=s,
                                         profile=self.motion_profile, ly=ly, rx=rx, source="ai", emit=self.emit)
            self.motion.record(obs.jpeg, ly, rx)
            self.curiosity.note_action(action)
            actions.append({"name": "drive", "args": {"action": action}, "result": res})
            await self.emit({"type": "tool_result", "name": "drive", "result": res, "ts": time.time()})
        elif action == "stop":
            await self._safe_stop()
        await self._set_status("idle")
        return {"ok": True, "actions": actions, "nav": True, "action": action}

    # --- modular vision brain (light VLM decides; Whisper hears; Piper speaks) ---
    async def _reason_vlm(self, trigger: str, s: Settings, obs: Observation, resting: bool) -> dict:
        client = VlmClient()
        frames = [base64.b64encode(obs.jpeg).decode()] if obs.jpeg else []
        heard = ""
        hl = self._heard_line(s)
        if hl:
            heard = str(self.ctx.heard.get("text", ""))
        # Describe (the slow ~3.5s scene sentence) only when spoken to, or every Nth roam cycle — otherwise do
        # fast nav so the robot keeps moving smoothly instead of pausing to narrate every step.
        self._vlm_cycle = getattr(self, "_vlm_cycle", 0) + 1
        # Describe the scene on every other autonomous cycle (was every 4th) so the robot visibly reasons
        # about what it sees, not just silently drives.
        describe = bool(heard) or trigger in ("speech", "manual", "touch") or (self._vlm_cycle % 2 == 0)
        with self.metrics.timer("vlm_decide"):
            decision = await client.decide(frames_b64=frames, mode=getattr(s, "mode", "explore"),
                                           heard=heard, describe=describe, persona=s.persona,
                                           robot_name=s.robot_name, directive=s.directive)
        spoken = (decision.get("text") or "").strip()
        action = (decision.get("action") or "none").lower()
        eyes = (decision.get("eyes") or "").lower()
        note = (decision.get("note") or "").strip()
        actions: list[dict] = []

        if note:
            await self.emit({"type": "thought", "text": f"({note})", "ts": time.time()})
        if spoken:
            await self.emit({"type": "thought", "text": spoken, "ts": time.time()})
            self._append_history({"role": "assistant", "content": spoken})
            # Smart memory: remember what FreeBo notices, but only when it's NOVEL — otherwise a slowly
            # changing view spams memory with near-duplicate "I see a wall" facts. Sightings of people are
            # always logged (cheap + useful). Best-effort; never blocks the loop.
            try:
                if decision.get("person"):
                    self.memory.log_sighting("person", kind="person", detail=spoken)
                elif describe:
                    novelty = self.curiosity.note_scene(spoken)
                    if novelty >= 0.5:
                        self.memory.remember(spoken, kind="observation")
            except Exception:  # noqa: BLE001
                pass

        if eyes:
            try:
                await self.link.action(f"eyes_{eyes}")
                actions.append({"name": "set_eyes", "args": {"animation": eyes}})
            except Exception:  # noqa: BLE001
                pass

        # Closed-loop reaction: if the last move got us nowhere (stuck/blocked) OR a reflex just stopped us for
        # something close ahead, don't push forward again — turn to find a clear path instead.
        if self._stuck_forward(action) or (self._reflex_blocked and action in ("forward", "back", "backward")):
            self._reflex_blocked = False
            action = "right" if (self._vlm_cycle % 2 == 0) else "left"
            self._motion_reaction = None
            await self.emit({"type": "thought", "text": "(blocked ahead — turning to find a clear path)",
                             "ts": time.time()})

        vec = self._calib_drive(action)
        if vec and self._motion_allowed(s, resting):
            ly, rx, _dur = vec
            await self._set_status("acting")
            await self.emit({"type": "tool_call", "name": "drive",
                             "args": {"action": action}, "ts": time.time()})
            # Closed-loop cerebellum owns the magnitudes/durations + camera-confirms the move.
            res = await locomotion.drive(link=self.link, safety=self.safety, settings=s,
                                         profile=self.motion_profile, ly=ly, rx=rx, source="ai", emit=self.emit)
            self.motion.record(obs.jpeg, ly, rx)
            self.curiosity.note_action(action)
            actions.append({"name": "drive", "args": {"action": action}, "result": res})
            await self.emit({"type": "tool_result", "name": "drive", "result": res, "ts": time.time()})
        elif action == "stop":
            await self._safe_stop()

        # Speak ONLY when there's something to say to a person — i.e. a real interaction (someone spoke to us,
        # a manual chat/tick, or a touch), never narrating observations on autonomous roam cycles. Routed
        # through the safety floor so the talk toggle + QUIET window are respected.
        intentional = bool(heard) or trigger in ("speech", "manual", "touch")
        if spoken and intentional and self.safety.check_say(s).allowed:
            await self._speak(spoken)

        return {"ok": True, "actions": actions, "vlm": True, "spoken": spoken, "action": action}

    async def _speak(self, text: str) -> None:
        from . import audio_state, tts
        from .speech_clean import clean_spoken
        text = clean_spoken(text)
        if not text:
            return
        self._last_spoken = text
        try:
            wav = tts.render_wav(text)
            if wav:
                audio_state.mark_speaking(audio_state.wav_duration_s(wav))
            # Robot speaker: native links that can publish audio into the call take the rendered WAV directly
            # (keeps TTS out of the robot layer). Others get the text (browser/cloud TTS path).
            pub = getattr(self.link, "publish_speech", None)
            if pub and wav:
                await pub(wav)
            elif self.link.prefers_text_tts():
                await self.link.say_text(text)
            if wav:
                await self.emit({"type": "speech", "text": text, "b64": base64.b64encode(wav).decode(),
                                 "sr": 0, "ts": time.time()})
        except Exception:  # noqa: BLE001
            pass

    # --- omni brain (MiniCPM-o: vision + audio + native speech, one model) ---
    # (ly, rx, duration). Forward bursts are long enough to bridge the gap between fast nav cycles so the
    # robot rolls continuously instead of lurch-stop-lurch (onboard avoidance + the deadman keep it safe).
    # Shorter forward bursts (1.2s, was 2.0s): onboard avoidance is OFF, so re-check the camera more often
    # instead of charging blind — far fewer bumps. Turns stay brief.
    _OMNI_DRIVE = {
        "forward": (0.7, 0.0, 1.2), "back": (-0.6, 0.0, 0.8), "backward": (-0.6, 0.0, 0.8),
        "left": (0.0, -0.7, 0.7), "right": (0.0, 0.7, 0.7),
    }

    def _omni_instruction(self, s: Settings, obs: Observation) -> str:
        name = s.robot_name or "FreeBo"
        bits = [
            f"You are {name}, a small two-wheeled robot with a camera, wheels, and a voice. {s.persona}",
            self._mode_prompt(s).replace("YOUR CURRENT MODE", "MODE"),
            "Look at the camera frame. React like a curious, friendly robot.",
        ]
        heard = self._heard_line(s)
        if heard:
            bits.append(heard)
        if obs.telemetry.get("touched"):
            bits.append("You were just touched/bumped — react with surprise and turn to look.")
        if resting_note := ("You are charging/docked right now — do NOT drive." if self._resting(obs.telemetry) else ""):
            bits.append(resting_note)
        bits.append(motion_model.guidance_text(s.robot_variant))
        bits.append(
            "Reply with ONE short, natural spoken sentence (it will be spoken aloud). Then on a NEW final "
            "line output exactly this control tag and nothing else:\n"
            "CMD action=<forward|back|left|right|stop|none> eyes=<neutral|happy|curious|surprised|love|"
            "sleepy|excited|confused>")
        return "\n".join(b for b in bits if b)

    def _parse_omni(self, text: str) -> tuple[str, str, str]:
        """Split the model's reply into (spoken_sentence, action, eyes)."""
        import re
        action, eyes = "none", ""
        spoken_lines = []
        for line in (text or "").splitlines():
            m = re.search(r"\bCMD\b.*?action\s*=\s*([a-z_]+).*?eyes\s*=\s*([a-z_]+)", line, re.I)
            if m:
                action = m.group(1).lower()
                eyes = m.group(2).lower()
                continue
            if line.strip():
                spoken_lines.append(line.strip())
        return " ".join(spoken_lines).strip(), action, eyes

    async def _reason_omni(self, trigger: str, s: Settings, obs: Observation, resting: bool) -> dict:
        client = OmniClient()
        frames = [base64.b64encode(obs.jpeg).decode()] if obs.jpeg else []
        instruction = self._omni_instruction(s, obs)
        await self._set_status("thinking")
        with self.metrics.timer("omni"):
            reply = await client.respond(frames_b64=frames, instruction=instruction)
        text = (reply.get("text") or "").strip()
        spoken, action, eyes = self._parse_omni(text)
        actions: list[dict] = []

        if spoken:
            await self.emit({"type": "thought", "text": spoken, "ts": time.time()})
            self._append_history({"role": "assistant", "content": spoken})

        # eyes
        if eyes:
            try:
                await self.link.action(f"eyes_{eyes}")
                actions.append({"name": "set_eyes", "args": {"animation": eyes}})
            except Exception:  # noqa: BLE001
                pass

        # Closed-loop reaction: escape a confirmed-stuck forward by turning instead of repeating it.
        if self._stuck_forward(action):
            action = "right" if (getattr(self, "_vlm_cycle", 0) % 2 == 0) else "left"
            self._motion_reaction = None
            await self.emit({"type": "thought", "text": "(was stuck — turning to find a clear path)",
                             "ts": time.time()})

        # movement (gated by mode/toggles/resting exactly like the tool path)
        vec = self._calib_drive(action)
        if vec and self._motion_allowed(s, resting):
            ly, rx, _dur = vec
            await self._set_status("acting")
            await self.emit({"type": "tool_call", "name": "drive",
                             "args": {"action": action}, "ts": time.time()})
            res = await locomotion.drive(link=self.link, safety=self.safety, settings=s,
                                         profile=self.motion_profile, ly=ly, rx=rx, source="ai", emit=self.emit)
            self.motion.record(obs.jpeg, ly, rx)
            actions.append({"name": "drive", "args": {"action": action}, "result": res})
            await self.emit({"type": "tool_result", "name": "drive", "result": res, "ts": time.time()})
        elif action == "stop":
            await self._safe_stop()

        # native speech: MiniCPM-o generated the voice audio itself. Only play it on a real interaction (heard
        # /manual/touch), not as narration every roam cycle.
        intentional = bool(self._heard_line(s)) or trigger in ("speech", "manual", "touch")
        if spoken and intentional and s.talk_enabled and reply.get("speech_b64"):
            await self.emit({"type": "speech", "text": spoken, "b64": reply["speech_b64"],
                             "sr": reply.get("sr", 24000), "ts": time.time()})

        return {"ok": True, "actions": actions, "omni": True, "spoken": spoken, "action": action}

    # --- helpers ---
    def _observation_message(self, obs: Observation, s: Settings, prefix: str = "", resting: bool = False) -> dict:
        bits = [obs.text_summary()]
        if obs.caption or self.buffer.caption:
            bits.append("WHAT YOUR EYES SEE:\n" + (obs.caption or self.buffer.caption))
        if resting:
            bits.append("You are charging/docked/resting right now — do not drive; stay put and just observe "
                        "or reply.")
        if self._reflex_blocked:
            self._reflex_blocked = False
            bits.append("NOTE: a reflex just stopped you — an obstacle is very close ahead. TURN to a clear "
                        "direction; do NOT drive straight forward.")
        r = self._motion_reaction
        if r is not None and getattr(r, "state", "") in ("stuck", "blocked"):
            bits.append(f"NOTE: your last move appears {r.state} ({getattr(r, 'detail', '')}). If forward is "
                        f"blocked, TURN to find a clear path instead of repeating the same move.")
        heard = self._heard_line(s)
        if heard:
            bits.append(heard)
        bits.append("Decide your next action. Think briefly, then call tools.")
        text = (prefix + " " if prefix else "") + "\n\n".join(bits)
        content: list[dict] = [{"type": "text", "text": text}]
        # Text-only to the cortex when the VLM is the eyes (hybrid perception, and reachable), in classic
        # hybrid caption mode, or when video is off; otherwise attach the image for a vision-capable decision
        # model. When hybrid eyes are DOWN, _vlm_perception_active is False so we attach the frame (fallback).
        text_only = self._vlm_perception_active(s) or self._hybrid(s) or not s.allow_video
        url = None if text_only else obs.image_data_url()
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": "user", "content": content}

    def _append_history(self, msg: dict):
        self.history.append(msg)
        max_msgs = max(4, self.settings.history_turns * 3)
        if len(self.history) > max_msgs:
            self.history = self.history[-max_msgs:]
            while self.history and self.history[0].get("role") == "tool":
                self.history.pop(0)

    def _trimmed_history(self) -> list[dict]:
        hist = list(self.history)
        while hist and hist[0].get("role") == "tool":
            hist.pop(0)
        return hist

    async def _set_status(self, status: str, error: str | None = None):
        self.status = status
        if error is not None:
            self.last_error = error
        await self.emit({"type": "status", "status": status, "error": self.last_error, "ts": time.time()})

    async def _safe_stop(self):
        try:
            await self.link.stop()
        except Exception:  # noqa: BLE001
            pass

    # --- always-respected voice commands ---
    async def _apply_command(self, intent: str, text: str, s: Settings) -> None:
        """Apply a matched voice order (preempts normal reasoning). Side effects first, then for the
        non-terminal ones let the cortex acknowledge + act under the new behavior."""
        if intent == "STOP":
            await self._safe_stop()
            self.behavior.set_voice_intent("stopped", seconds=3600.0)  # stay put until told to move again
            await self.emit({"type": "thought", "text": "(stopping — holding position)", "ts": time.time()})
            await self._set_status("idle", "stopped (you told me to)")
            return
        if intent == "QUIET":
            self.safety.set_quiet(120.0)
            await self.emit({"type": "thought", "text": "(quieted — staying silent)", "ts": time.time()})
            await self._express("neutral")
            return
        if intent == "SLEEP":
            await self._go_dark()
            return
        if intent == "SPEAK_UP":
            self.safety.set_quiet(0.0)
            if self._last_spoken and s.talk_enabled:
                await self._speak(self._last_spoken)
            return
        if intent == "BACK_UP":
            await self._back_up(s)
            return
        if intent == "HOME":
            self.behavior.set_voice_intent("return", seconds=120.0)
            with contextlib.suppress(Exception):
                await self.link.action("dock")
            await self.emit({"type": "thought", "text": "(heading home to dock)", "ts": time.time()})
            return
        if intent == "EXPLORE":
            self.settings.update(mode="explore", autonomy="auto")
            self.behavior.set_voice_intent("explore", seconds=240.0)
            await self.emit({"type": "settings", "changed": ["mode", "autonomy"],
                             "settings": self.settings.public_dict()})
            await self._reason("command", self.settings.snapshot())
            return
        if intent == "COME":
            directive = "Come to the person who called you: find them with your camera, drive over, stay near."
            self.settings.update(mode="command", directive=directive, autonomy="auto")
            self.behavior.set_voice_intent("pursue", seconds=120.0, detail=directive)
            await self.emit({"type": "settings", "changed": ["mode", "directive", "autonomy"],
                             "settings": self.settings.public_dict()})
            await self._reason("command", self.settings.snapshot())
            return

    async def _go_dark(self) -> None:
        self.settings.update(asleep=True, autonomy="manual")
        await self._safe_stop()
        with contextlib.suppress(Exception):
            await self.link.connection("stop")
        await self.emit({"type": "settings", "changed": ["asleep", "autonomy"],
                         "settings": self.settings.public_dict()})
        await self._set_status("asleep", "going dark (voice)")

    async def _back_up(self, s: Settings) -> None:
        """A short reverse + turn to un-stick. Explicit command -> source='manual' (bypasses scope/autonomy,
        still speed/duration-clamped)."""
        await self.emit({"type": "thought", "text": "(backing up to get unstuck)", "ts": time.time()})
        for ly, rx, dur in ((-0.5, 0.0, 0.6), (0.0, 0.6, 0.5)):
            d = self.safety.check_drive(self.settings.snapshot(), ly, rx, dur, source="manual")
            if d.allowed:
                with contextlib.suppress(Exception):
                    await self.link.move(d.ly, d.rx, d.duration)
                await asyncio.sleep(dur + 0.2)
        await self._safe_stop()
        self._motion_reaction = None
        self._reflex_blocked = False
        self.behavior.clear_voice_intent()   # free to move again

    def status_dict(self) -> dict:
        s = self.settings.snapshot()
        return {
            "status": self.status,
            "error": self.last_error,
            "last_tick_ts": self.last_tick_ts,
            "autonomy": self.settings.autonomy,
            "running": self._running,
            "motion_state": self._motion_state,
            "calibrated": self.motion_profile is not None,
            "behavior": self.behavior.state(),
            "curiosity": self.curiosity.state(),
            "skills": [sk.name for sk in self.registry.active_skills()],
            # Brain architecture + hybrid 'eyes' health (docs/MATURITY.md §1) and a compact latency view (§2).
            "brain_mode": brain_mode(s),
            "vlm_ok": self._vlm_ok,
            "metrics": self.metrics.summary(),
        }
