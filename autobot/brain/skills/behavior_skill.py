"""Behavior skill — lets the cortex honor PARAPHRASED commands the keyword matcher misses.

The fast matcher in `commands.py` catches the obvious imperatives ("stop", "go explore", ...). This skill
gives the LLM the same levers as tools so natural-language requests work too ("could you wander off and
check the kitchen", "settle down and just watch", "hush for a bit"). Movement-mode changes are owner-gated
(the spoken-owner policy); quieting/staying are harmless and open to anyone.
"""
from __future__ import annotations

from .base import Skill, SkillContext, ToolDef, fn_schema


class BehaviorSkill(Skill):
    name = "behavior"

    def available(self, ctx: SkillContext) -> tuple[bool, str]:
        return (getattr(ctx, "behavior", None) is not None, "no behavior controller")

    def system_prompt_fragment(self, ctx: SkillContext) -> str:
        return ("BEHAVIOR CONTROLS: obey spoken requests about what to do. To start roaming/exploring call "
                "`set_mode('explore')`; to just watch from here call `stay`; to go to someone call "
                "`come_here`; to go charge call `dock`; to stop call `stop`; to hush call `be_quiet`.")

    def tools(self, ctx: SkillContext) -> list[ToolDef]:
        return [
            ToolDef(fn_schema("set_mode", "Change what you're doing: observe (stay put and watch), explore (roam the home), conversational (stay put and chat), or command (pursue a directive). Use when asked to go explore / settle down / just watch / follow an order.", {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["observe", "explore", "conversational", "command"]},
                    "directive": {"type": "string", "description": "For command mode: what to pursue."},
                },
                "required": ["mode"],
            }), self._make_set_mode(ctx), authority="owner"),
            ToolDef(fn_schema("stay", "Stop roaming and hold position (you may still look around and talk). Use when told to stay / settle down / wait here.", {"type": "object", "properties": {}}),
                    self._make_stay(ctx), authority="anyone"),
            ToolDef(fn_schema("come_here", "Go to the person you're talking with: find them and drive over, then stay near.", {"type": "object", "properties": {}}),
                    self._make_come(ctx), authority="owner"),
            ToolDef(fn_schema("be_quiet", "Stop talking for a while (stay silent). Use when told to hush / be quiet.", {
                "type": "object", "properties": {"seconds": {"type": "number", "default": 120}},
            }), self._make_quiet(ctx), authority="anyone"),
        ]

    def _make_set_mode(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            mode = str(a.get("mode", "")).strip()
            if mode not in ("observe", "explore", "conversational", "command"):
                return {"ok": False, "error": "mode must be observe|explore|conversational|command"}
            changes = {"mode": mode}
            if mode in ("explore", "command"):
                changes["autonomy"] = "auto"   # actually act on it (per the spoken-owner policy)
            if mode == "command" and a.get("directive"):
                changes["directive"] = str(a.get("directive"))
            ctx.settings.update(**changes)
            if mode == "explore":
                ctx.behavior.set_voice_intent("explore", seconds=240.0)
            elif mode == "command":
                ctx.behavior.set_voice_intent("pursue", seconds=120.0, detail=str(a.get("directive", "")))
            else:
                ctx.behavior.clear_voice_intent()
            return {"ok": True, "mode": mode}
        return h

    def _make_stay(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            ctx.behavior.set_voice_intent("stopped", seconds=3600.0)
            try:
                await ctx.link.stop()
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "staying": True}
        return h

    def _make_come(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            directive = "Come to the person you're talking with: find them, drive over, stay near."
            ctx.settings.update(mode="command", directive=directive, autonomy="auto")
            ctx.behavior.set_voice_intent("pursue", seconds=120.0, detail=directive)
            return {"ok": True, "coming": True}
        return h

    def _make_quiet(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            secs = max(5.0, min(float(a.get("seconds", 120) or 120), 1800.0))
            ctx.safety.set_quiet(secs)
            return {"ok": True, "quiet_seconds": secs}
        return h
