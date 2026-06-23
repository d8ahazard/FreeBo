"""Patrol skill — "smart patrol" layered on the robot's native patrol toggle + the AI's vision.

The robot has a native `security_patrol/enable` param (handled by core's set_toggle). This skill adds the
*smart* part: a patrol mode the AI runs on its normal tick — rove between saved places, watch for people /
pets / motion, and log what it sees as patrol events (kept in memory + emitted to the UI). It also nudges
the agent (via the system prompt) on how to patrol when active.

No new motion path: patrol uses the existing safety-clamped drive + the places skill, so the safety floor
and deadman still apply. Scheduling is the agent's tick loop (set autonomy to `auto` to patrol hands-free).
"""
from __future__ import annotations

import time

from .base import Skill, SkillContext, ToolDef, fn_schema


class PatrolSkill(Skill):
    name = "patrol"

    def __init__(self):
        self.active = False
        self.started = 0.0
        self.interval_s = 60.0
        self.events: list[dict] = []
        self._last_event_emit = 0.0

    def system_prompt_fragment(self, ctx: SkillContext) -> str:
        if not self.active:
            return ("PATROL: use `start_patrol` to begin a security patrol (you'll rove and watch for "
                    "people/pets/changes).")
        return ("PATROL IS ACTIVE: every step, look around, move gently between saved places (see `places`), "
                "and use `note_sighting` / `send_alert` for anything notable (a person, a pet, motion, an open "
                "door). Keep moves small and safe. Use `stop_patrol` when asked to stop.")

    def tools(self, ctx: SkillContext) -> list[ToolDef]:
        return [
            ToolDef(fn_schema("start_patrol", "Begin a smart security patrol (rove + watch for people/pets/motion).", {
                "type": "object",
                "properties": {"interval_seconds": {"type": "number", "default": 60,
                                                    "description": "Roughly how often to move to a new vantage."}},
            }), self._make_start(ctx), authority="owner"),
            ToolDef(fn_schema("stop_patrol", "Stop the security patrol.", {"type": "object", "properties": {}}),
                    self._make_stop(ctx), authority="owner"),
            ToolDef(fn_schema("patrol_status", "Report whether a patrol is active and recent patrol events.", {"type": "object", "properties": {}}),
                    self._make_status(ctx), authority="anyone"),
        ]

    def _make_start(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            self.active = True
            self.started = time.time()
            self.interval_s = max(10.0, float(a.get("interval_seconds", 60)))
            # turn on the robot's native patrol assist too (best-effort)
            try:
                await ctx.link.action("patrol_on")
            except Exception:  # noqa: BLE001
                pass
            await ctx.emit({"type": "patrol", "active": True, "ts": time.time()})
            ctx.memory.remember("Started a security patrol.", kind="event", source="owner")
            return {"ok": True, "patrolling": True, "interval_s": self.interval_s}
        return h

    def _make_stop(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            self.active = False
            try:
                await ctx.link.action("patrol_off")
                await ctx.link.stop()
            except Exception:  # noqa: BLE001
                pass
            await ctx.emit({"type": "patrol", "active": False, "ts": time.time()})
            return {"ok": True, "patrolling": False}
        return h

    def _make_status(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            return {"ok": True, "active": self.active,
                    "since": self.started if self.active else None,
                    "recent_events": self.events[-10:]}
        return h

    async def on_observe(self, ctx: SkillContext, observation) -> None:
        """While patrolling, record people present as patrol events (recognition feeds identity)."""
        if not self.active:
            return
        try:
            present = ctx.identity.present_people()
        except Exception:  # noqa: BLE001
            present = []
        if present:
            ev = {"ts": time.time(), "kind": "person", "detail": ", ".join(present)}
            self.events.append(ev)
            self.events = self.events[-50:]
            now = time.time()
            if now - self._last_event_emit > 10:
                self._last_event_emit = now
                await ctx.emit({"type": "patrol_event", **ev})
