"""Memory skill — lets the AI persist and recall facts, and injects what it remembers into the prompt."""
from __future__ import annotations

import asyncio

from .base import Skill, SkillContext, ToolDef, fn_schema


class MemorySkill(Skill):
    name = "memory"

    def system_prompt_fragment(self, ctx: SkillContext) -> str:
        return ctx.memory.summary_for_prompt()

    def tools(self, ctx: SkillContext) -> list[ToolDef]:
        return [
            ToolDef(fn_schema("remember", "Save something to long-term memory so you recall it in future sessions (owner preferences, names, places, important events).", {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The fact to remember, phrased so future-you understands it."},
                    "kind": {"type": "string", "enum": ["fact", "preference", "person", "place", "event"], "default": "fact"},
                },
                "required": ["text"],
            }), self._make_remember(ctx), authority="anyone"),
            ToolDef(fn_schema("recall", "Search your long-term memory for things you've remembered.", {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to look up."}},
                "required": ["query"],
            }), self._make_recall(ctx), authority="anyone"),
            ToolDef(fn_schema("forget", "Remove remembered facts that match a phrase.", {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }), self._make_forget(ctx), authority="anyone"),
        ]

    def _make_remember(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            f = ctx.memory.remember(str(a.get("text", "")), kind=str(a.get("kind", "fact")), source="ai")
            return {"ok": bool(f.text), "remembered": f.text, "kind": f.kind}
        return h

    def _make_recall(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            # recall may embed via a network call (semantic mode) — run it off the event loop.
            hits = await asyncio.to_thread(ctx.memory.recall, str(a.get("query", "")), 5)
            return {"ok": True, "recalled": [{"text": f.text, "kind": f.kind} for f in hits]}
        return h

    def _make_forget(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            n = ctx.memory.forget(str(a.get("query", "")))
            return {"ok": True, "forgot": n}
        return h
