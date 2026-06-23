"""Notifications — push FreeBo alerts out to the owner (long-distance / pets / kids / elderly use-cases).

Channels (all optional, fail-soft):
  - the UI (always): emits an `alert` event over the WebSocket.
  - a webhook: set AUTOBOT_WEBHOOK_URL (Discord/Slack/ntfy/Home Assistant webhook/etc.) — we POST JSON.
  - Home Assistant: the existing MQTT integration (native mode) surfaces robot state separately.

Kept tiny and dependency-light (httpx is already a dep). Recent alerts are retained in-memory for the UI.
"""
from __future__ import annotations

import os
import time
from collections import deque
from typing import Awaitable, Callable

_RECENT: deque[dict] = deque(maxlen=50)


def recent() -> list[dict]:
    return list(_RECENT)


def webhook_url() -> str:
    return os.environ.get("AUTOBOT_WEBHOOK_URL", "").strip()


async def send(emit: Callable[[dict], Awaitable[None]] | None, message: str,
               level: str = "info", source: str = "freebo") -> dict:
    """Fan an alert out to the UI + webhook. Returns a small result dict; never raises."""
    msg = (message or "").strip()
    if not msg:
        return {"ok": False, "error": "empty message"}
    evt = {"type": "alert", "message": msg, "level": level, "source": source, "ts": time.time()}
    _RECENT.append(evt)
    if emit:
        try:
            await emit(evt)
        except Exception:  # noqa: BLE001
            pass
    sent_webhook = False
    url = webhook_url()
    if url:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8) as c:
                # Common shape: many services accept {"content"/"text"/"message"}. Send all three.
                await c.post(url, json={"content": msg, "text": msg, "message": msg,
                                        "level": level, "source": source})
            sent_webhook = True
        except Exception:  # noqa: BLE001 - webhook failures must not break anything
            sent_webhook = False
    return {"ok": True, "ui": True, "webhook": sent_webhook}
