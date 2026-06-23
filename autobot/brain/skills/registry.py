"""The skill registry: composes enabled skills, builds the tool list, and dispatches calls through the
owner-authority gate. The per-tool safety clamps live inside each handler (which calls `ctx.safety`)."""
from __future__ import annotations

import threading

from .base import Skill, SkillContext, ToolDef


def build_default_skills() -> list[Skill]:
    """Instantiate the built-in skills. Each decides at runtime whether it's active (graceful degradation),
    so importing one whose optional dependency is missing must never crash."""
    from .behavior_skill import BehaviorSkill
    from .core import CoreSkill
    from .memory_skill import MemorySkill
    skills: list[Skill] = [CoreSkill(), MemorySkill(), BehaviorSkill()]
    for modname, cls in (("home_assistant", "HomeAssistantSkill"),
                         ("recognition", "RecognitionSkill"),
                         ("voice", "VoiceSkill"),
                         ("places", "PlacesSkill"),
                         ("patrol", "PatrolSkill"),
                         ("alerts", "AlertsSkill"),
                         ("tasks_skill", "TasksSkill"),
                         ("mcp_skill", "McpSkill")):
        try:
            mod = __import__(f"autobot.brain.skills.{modname}", fromlist=[cls])
            skills.append(getattr(mod, cls)())
        except Exception as e:  # noqa: BLE001 - an optional skill failing to import must not break the app
            print(f"[skills] {modname} unavailable: {type(e).__name__}: {e}", flush=True)
    return skills


class SkillRegistry:
    def __init__(self, skills: list[Skill], ctx: SkillContext):
        self.skills = skills
        self.ctx = ctx
        self._bg_started = False

    def active_skills(self) -> list[Skill]:
        out = []
        for sk in self.skills:
            try:
                ok, _ = sk.available(self.ctx)
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                out.append(sk)
        return out

    def _tool_map(self) -> dict[str, ToolDef]:
        m: dict[str, ToolDef] = {}
        for sk in self.active_skills():
            try:
                for td in sk.tools(self.ctx):
                    m[td.schema["function"]["name"]] = td
            except Exception as e:  # noqa: BLE001
                print(f"[skills] {sk.name} tools() failed: {e}", flush=True)
        return m

    def schemas(self, eye_animations: list[str], exclude: set[str] | None = None) -> list[dict]:
        self.ctx.eye_animations = eye_animations
        exclude = exclude or set()
        return [td.schema for name, td in self._tool_map().items() if name not in exclude]

    def system_prompt_additions(self) -> str:
        parts = []
        for sk in self.active_skills():
            try:
                frag = sk.system_prompt_fragment(self.ctx)
            except Exception:  # noqa: BLE001
                frag = ""
            if frag:
                parts.append(frag.strip())
        return "\n\n".join(parts)

    async def on_observe(self, observation) -> None:
        for sk in self.active_skills():
            try:
                await sk.on_observe(self.ctx, observation)
            except Exception as e:  # noqa: BLE001 - a skill hook must never break the loop
                print(f"[skills] {sk.name} on_observe failed: {e}", flush=True)

    def start_background(self):
        if self._bg_started:
            return
        self._bg_started = True
        for sk in self.skills:
            try:
                ok, _ = sk.available(self.ctx)
                if not ok:
                    continue
                for worker in sk.background_workers(self.ctx):
                    threading.Thread(target=worker, name=f"skill-{sk.name}", daemon=True).start()
            except Exception as e:  # noqa: BLE001
                print(f"[skills] {sk.name} background start failed: {e}", flush=True)

    async def execute(self, name: str, args: dict) -> dict:
        """Run one tool call from the AI. Enforces the owner-authority gate, then the handler (which applies
        its own safety clamps). Never raises."""
        td = self._tool_map().get(name)
        if td is None:
            return {"ok": False, "error": f"unknown tool '{name}'"}
        s = self.ctx.settings.snapshot()
        if td.authority == "owner" and not self.ctx.identity.authority_active(s):
            present = self.ctx.identity.present_people()
            requester = next((p for p in present if not self.ctx.identity.is_owner(p, s)), None) or "someone"
            await self.ctx.identity.request_approval(
                name, args, requester,
                reason=f"{requester} requested '{name}', but owner authority is not active")
            return {"ok": False, "blocked": "awaiting owner approval", "pending": True,
                    "note": f"Asked {s.owner_name or 'the owner'} to approve."}
        try:
            return await td.handler(args or {})
        except Exception as e:  # noqa: BLE001 - tools must never crash the loop
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
