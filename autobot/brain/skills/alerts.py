"""Alerts skill — lets the AI notify the owner (motion, a person/pet seen, something wrong).

Wraps autobot/brain/notify.py: emits to the UI and, if AUTOBOT_WEBHOOK_URL is set, to a webhook
(Discord/Slack/ntfy/Home Assistant/etc.). This is what powers the long-distance / pets / kids / elderly
"tell me if something happens" use-cases, combined with the patrol skill.
"""
from __future__ import annotations

from .. import notify
from .base import Skill, SkillContext, ToolDef, fn_schema


class AlertsSkill(Skill):
    name = "alerts"

    def system_prompt_fragment(self, ctx: SkillContext) -> str:
        extra = " (a webhook is configured)" if notify.webhook_url() else ""
        return (f"ALERTS: use `send_alert` to notify the owner about something noteworthy — a person/pet "
                f"seen, motion, an open door, low battery{extra}. Keep alerts meaningful, not spammy.")

    def tools(self, ctx: SkillContext) -> list[ToolDef]:
        return [
            ToolDef(fn_schema("send_alert", "Notify the owner (UI + webhook if configured) about something noteworthy.", {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "What happened, in one short sentence."},
                    "level": {"type": "string", "enum": ["info", "warning", "urgent"], "default": "info"},
                },
                "required": ["message"],
            }), self._make_send(ctx), authority="anyone"),
        ]

    def _make_send(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            res = await notify.send(ctx.emit, str(a.get("message", "")),
                                    level=str(a.get("level", "info")), source="freebo-ai")
            if res.get("ok"):
                ctx.memory.remember(f"Alerted owner: {a.get('message','')}", kind="event", source="ai")
            return res
        return h
