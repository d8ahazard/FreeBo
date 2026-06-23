"""Tasks skill — let the AI (or the user, via it) schedule reminders and recurring jobs.

The robot can set itself things to do later or on a schedule: a one-shot reminder ("in 20 minutes, tell me
to take the pizza out"), a daily routine ("every day at 08:00, patrol the house and greet whoever's up"), or
a repeating check. When a task fires, the brain's scheduler injects its text as a high-priority instruction
and the robot acts on it through the normal safety floor.

The store is owned by the brain (ctx.tasks); this skill just exposes add/list/cancel tools. Active only when
the brain wired a task store (always, in practice).
"""
from __future__ import annotations

from .base import Skill, SkillContext, ToolDef, fn_schema


class TasksSkill(Skill):
    name = "tasks"

    def available(self, ctx: SkillContext) -> tuple[bool, str]:
        return (getattr(ctx, "tasks", None) is not None, "no task store")

    def system_prompt_fragment(self, ctx: SkillContext) -> str:
        base = ("TASKS: you can schedule things with `add_task` — a one-shot reminder (`in_seconds`), a daily "
                "routine (`daily_time` 'HH:MM'), or a repeat (`every_seconds`). The task `text` is the "
                "instruction you'll be given when it fires (write it as a directive to yourself, e.g. 'Drive "
                "to the kitchen and tell Dave dinner is ready'). Use `list_tasks` / `cancel_task` to manage them.")
        sched = ctx.tasks.summary_for_prompt() if getattr(ctx, "tasks", None) else ""
        return base + ("\n" + sched if sched else "")

    def tools(self, ctx: SkillContext) -> list[ToolDef]:
        return [
            ToolDef(fn_schema("add_task", "Schedule a task/reminder for later. Set exactly one of in_seconds, daily_time, or every_seconds.", {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "What to do when it fires (a directive to yourself)."},
                    "in_seconds": {"type": "number", "description": "One-shot: fire this many seconds from now."},
                    "daily_time": {"type": "string", "description": "Repeat daily at this local time, 'HH:MM'."},
                    "every_seconds": {"type": "number", "description": "Repeat on this interval (seconds)."},
                },
                "required": ["text"],
            }), self._make_add(ctx), authority="owner"),
            ToolDef(fn_schema("list_tasks", "List your scheduled tasks and reminders.", {"type": "object", "properties": {}}),
                    self._make_list(ctx), authority="anyone"),
            ToolDef(fn_schema("cancel_task", "Cancel a scheduled task by its id.", {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            }), self._make_cancel(ctx), authority="owner"),
        ]

    def _make_add(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            text = str(a.get("text", "")).strip()
            if not text:
                return {"ok": False, "error": "text required"}
            t = ctx.tasks.add(text, in_seconds=a.get("in_seconds"), daily_time=a.get("daily_time"),
                              every_seconds=a.get("every_seconds"))
            ctx.memory.remember(f"Scheduled task: {text} ({t.schedule_label()}).", kind="event", source="owner")
            return {"ok": True, "task_id": t.id, "schedule": t.schedule_label(), "next_run": t.next_run}
        return h

    def _make_list(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            tasks = [{"id": t.id, "text": t.text, "schedule": t.schedule_label(),
                      "next_run": t.next_run, "enabled": t.enabled, "runs": t.runs} for t in ctx.tasks.list()]
            return {"ok": True, "tasks": tasks}
        return h

    def _make_cancel(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            return {"ok": ctx.tasks.cancel(str(a.get("task_id", "")))}
        return h
