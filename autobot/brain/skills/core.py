"""Core skill — the robot's built-in body controls (the original closed tool set).

These map directly to `RobotLink` verbs and keep the safety floor inline (each motion handler calls
`ctx.safety`). Physical/state-changing tools are authority "owner" so the obedience policy can gate them;
harmless tools (stop, look, wait, set_eyes, say) are "anyone". `say` is additionally gated by the talk toggle.
"""
from __future__ import annotations

import time

from .. import locomotion, tts
from .base import Skill, SkillContext, ToolDef, fn_schema

# Unit drive vectors. ly>0 forward; rx>0 turns right, rx<0 turns left (matches motor_frame).
DIRECTIONS: dict[str, tuple[float, float]] = {
    "forward": (1.0, 0.0), "back": (-1.0, 0.0), "backward": (-1.0, 0.0),
    "left": (0.0, -1.0), "right": (0.0, 1.0),
    "forward_left": (0.8, -0.6), "forward_right": (0.8, 0.6),
    "back_left": (-0.8, -0.6), "back_right": (-0.8, 0.6),
    "spin_left": (0.0, -1.0), "spin_right": (0.0, 1.0),
}

TOGGLE_FEATURES = ("night", "avoid", "fall", "patrol", "eyes")


class CoreSkill(Skill):
    name = "core"

    def tools(self, ctx: SkillContext) -> list[ToolDef]:
        eyes_enum = sorted(set(ctx.eye_animations) | {"on", "off"})
        return [
            ToolDef(fn_schema("drive", "Move the robot ONE controlled increment in a direction, then auto-stop. A low-level motor controller picks the exact speed, turns IN SMALL increments, and confirms the move with the camera — so just give a direction and re-check your eyes after. Turning ('left'/'right') pivots in place; 'forward' takes one short step (only when the floor ahead is clearly open). You have NO bumper/distance sensor.", {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": sorted(DIRECTIONS.keys()),
                                  "description": "Which way to move. 'left'/'right' pivot in place; 'forward' is one short step."},
                    "ly": {"type": "number", "description": "Optional raw forward/back intent -1..1 (sign only; the controller sets the speed)."},
                    "rx": {"type": "number", "description": "Optional raw turn intent -1..1 (sign only; the controller sets the speed)."},
                },
            }), self._drive, authority="owner"),
            ToolDef(fn_schema("stop", "Stop all movement immediately.", {"type": "object", "properties": {}}),
                    self._stop, authority="anyone"),
            ToolDef(fn_schema("look", "Request a fresh camera frame to inspect on the next step.", {"type": "object", "properties": {}}),
                    self._look, authority="anyone"),
            ToolDef(fn_schema("say", "Speak text aloud through the robot's speaker (only if the user enabled talk).", {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "What to say. Keep it short and natural."}},
                "required": ["text"],
            }), self._say, authority="anyone"),
            ToolDef(fn_schema("set_eyes", "Set the eye expression/animation (or on/off).", {
                "type": "object",
                "properties": {"animation": {"type": "string", "enum": eyes_enum}},
                "required": ["animation"],
            }), self._set_eyes, authority="anyone"),
            ToolDef(fn_schema("set_toggle", "Turn a robot feature on or off.", {
                "type": "object",
                "properties": {"feature": {"type": "string", "enum": list(TOGGLE_FEATURES)}, "on": {"type": "boolean"}},
                "required": ["feature", "on"],
            }), self._set_toggle, authority="owner"),
            ToolDef(fn_schema("dock", "Send the robot to its charging dock.", {"type": "object", "properties": {}}),
                    self._dock, authority="owner"),
            ToolDef(fn_schema("undock", "Drive off the dock.", {"type": "object", "properties": {}}),
                    self._undock, authority="owner"),
            ToolDef(fn_schema("wake", "Wake the robot (camera/stream on).", {"type": "object", "properties": {}}),
                    self._wake, authority="owner"),
            ToolDef(fn_schema("sleep", "Put the robot to sleep.", {"type": "object", "properties": {}}),
                    self._sleep, authority="owner"),
            ToolDef(fn_schema("wait", "Do nothing this step and observe.", {
                "type": "object", "properties": {"seconds": {"type": "number", "default": 2}},
            }), self._wait, authority="anyone"),
        ]

    def __init__(self):
        self.ctx: SkillContext | None = None

    async def _drive(self, a: dict) -> dict:
        ctx = self._c()
        s = ctx.settings.snapshot()
        prof = getattr(ctx, "motion_profile", None)
        # We only take the INTENT (direction). The cerebellum (locomotion) owns the actual speeds/durations —
        # this robot's drivetrain has big deadbands + a touchy turn, so raw magnitudes are unreliable (see
        # docs/MOTION.md). It re-clamps through the safety floor and confirms motion via the camera.
        if a.get("ly") is not None or a.get("rx") is not None:
            ly, rx = float(a.get("ly", 0.0)), float(a.get("rx", 0.0))
        else:
            dx = DIRECTIONS.get(str(a.get("direction", "")).lower())
            if dx is None:
                return {"ok": False, "error": "need a valid direction or ly/rx"}
            ly, rx = dx
        res = await locomotion.drive(link=ctx.link, safety=ctx.safety, settings=s, profile=prof,
                                     ly=ly, rx=rx, source="ai", emit=ctx.emit)
        if res.get("blocked"):
            return {"ok": False, "blocked": res["blocked"]}
        return res

    async def _stop(self, a: dict) -> dict:
        res = await self._c().link.stop()
        return {"ok": res.get("ok", False), "stopped": True}

    async def _look(self, a: dict) -> dict:
        self._c().flags["look"] = True
        return {"ok": True, "note": "fresh frame will be attached next step"}

    async def _say(self, a: dict) -> dict:
        ctx = self._c()
        s = ctx.settings.snapshot()
        d = ctx.safety.check_say(s)
        if not d.allowed:
            return {"ok": False, "blocked": d.reason}
        text = str(a.get("text", "")).strip()
        if not text:
            return {"ok": False, "error": "empty text"}
        # Speak via the best path for this link:
        #  - Air 2 native (RTC): render WAV and publish_speech() onto the robot's speaker (+ emit for the UI).
        #  - Cloud/browser links: hand them the text (browser fetches TTS and publishes into Agora).
        #  - Local G.711 links (SE native/x86): render mulaw here and push it to the speaker.
        pub = getattr(ctx.link, "publish_speech", None)
        if callable(pub):
            wav = tts.render_wav(text)
            if wav:
                # Echo gate: mute STT for the clip duration (+tail) so the robot doesn't transcribe itself.
                from .. import audio_state
                audio_state.mark_speaking(audio_state.wav_duration_s(wav))
                res = await pub(wav)
                try:
                    import base64
                    await ctx.emit({"type": "speech", "text": text,
                                    "b64": base64.b64encode(wav).decode(), "sr": 0, "ts": time.time()})
                except Exception:  # noqa: BLE001
                    pass
            else:
                res = await ctx.link.say_text(text)
        elif ctx.link.prefers_text_tts():
            res = await ctx.link.say_text(text)
        else:
            g711 = tts.render_mulaw(text)
            res = await ctx.link.say_audio(g711, codec="mulaw") if g711 else await ctx.link.say_text(text)
        return {"ok": res.get("ok", False), "said": text[:80], "available": res.get("available")}

    async def _set_eyes(self, a: dict) -> dict:
        anim = str(a.get("animation", "")).lower()
        res = await self._c().link.action(f"eyes_{anim}")
        return {"ok": res.get("ok", False), "eyes": anim}

    async def _set_toggle(self, a: dict) -> dict:
        feat = str(a.get("feature", "")).lower()
        if feat not in TOGGLE_FEATURES:
            return {"ok": False, "error": f"unknown feature '{feat}'"}
        on = bool(a.get("on", True))
        res = await self._c().link.action(f"{feat}_{'on' if on else 'off'}")
        return {"ok": res.get("ok", False), "feature": feat, "on": on}

    async def _dock(self, a: dict) -> dict:
        return {"ok": (await self._c().link.action("dock")).get("ok", False), "docking": True}

    async def _undock(self, a: dict) -> dict:
        return {"ok": (await self._c().link.action("undock")).get("ok", False), "undocking": True}

    async def _wake(self, a: dict) -> dict:
        return {"ok": (await self._c().link.action("wake")).get("ok", False), "waking": True}

    async def _sleep(self, a: dict) -> dict:
        return {"ok": (await self._c().link.action("sleep")).get("ok", False), "sleeping": True}

    async def _wait(self, a: dict) -> dict:
        secs = max(0.0, min(float(a.get("seconds", 2)), 30.0))
        return {"ok": True, "waited": secs}

    # The registry calls tools(ctx) each time it builds the map, so stash the latest ctx for handlers.
    def _c(self) -> SkillContext:
        assert self.ctx is not None, "CoreSkill context not set"
        return self.ctx

    def available(self, ctx: SkillContext):
        self.ctx = ctx
        return True, ""
