"""Home Assistant skill — lets the AI control your smart home (the 'Jarvis' bit).

Talks to HA's REST API with a long-lived access token. Configure via env:
  HASS_BASE_URL   e.g. http://homeassistant.local:8123
  HASS_TOKEN      a long-lived access token (Profile -> Security in HA)

The skill is only active when both are set (graceful degradation). It can: list/search entities (cached),
read an entity's state, do simple on/off/toggle (with optional service data like brightness/color), and call
ANY service (scenes, scripts, climate, media, ...). Control is authority "owner" so the obedience policy
gates it. This is the robot controlling HA; HA controlling the robot is the separate MQTT path in
autobot/robot/mqtt.py. (For a tool-only integration you can instead point AUTOBOT_MCP_SERVERS at HA's MCP
Server integration — see docs/AI_BRAIN.md.)
"""
from __future__ import annotations

import os
import time

import httpx

from .base import Skill, SkillContext, ToolDef, fn_schema

BASE_URL = os.environ.get("HASS_BASE_URL", "").rstrip("/")
TOKEN = os.environ.get("HASS_TOKEN", "")
_CACHE_TTL = 10.0   # seconds to cache /api/states (avoids hammering HA when the model lists repeatedly)


class HomeAssistantSkill(Skill):
    name = "home_assistant"

    def __init__(self):
        self._states_cache: list[dict] = []
        self._states_ts = 0.0

    def available(self, ctx: SkillContext) -> tuple[bool, str]:
        if not (BASE_URL and TOKEN):
            return False, "HASS_BASE_URL / HASS_TOKEN not set"
        return True, ""

    def system_prompt_fragment(self, ctx: SkillContext) -> str:
        return ("HOME ASSISTANT: you can control the smart home. `list_entities` (optionally by domain or a "
                "name search) to find ids, `get_state` to check one, `home_assistant` to turn things on/off/"
                "toggle (with optional data like brightness_pct or color_name), and `ha_service` to call ANY "
                "service — scenes (scene.turn_on), scripts (script.turn_on), climate (climate.set_temperature), "
                "media, etc. List or search first if unsure of an entity id.")

    def tools(self, ctx: SkillContext) -> list[ToolDef]:
        return [
            ToolDef(fn_schema("list_entities", "List/search Home Assistant entities. Filter by domain (e.g. 'light','scene','climate') and/or a name search.", {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "e.g. light, switch, scene, script, climate."},
                    "search": {"type": "string", "description": "Optional substring to match in id or friendly name."},
                },
            }), self._list, authority="anyone"),
            ToolDef(fn_schema("get_state", "Read the current state (and key attributes) of one HA entity, e.g. to check if a door is open or a light is on.", {
                "type": "object",
                "properties": {"entity_id": {"type": "string", "description": "e.g. binary_sensor.front_door."}},
                "required": ["entity_id"],
            }), self._get_state, authority="anyone"),
            ToolDef(fn_schema("home_assistant", "Turn a device on/off/toggle. Optional `data` passes extra service params (e.g. {\"brightness_pct\":40} or {\"color_name\":\"red\"}).", {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "e.g. light.living_room."},
                    "action": {"type": "string", "enum": ["turn_on", "turn_off", "toggle"], "default": "toggle"},
                    "data": {"type": "object", "description": "Optional extra service data."},
                },
                "required": ["entity_id"],
            }), self._call, authority="owner"),
            ToolDef(fn_schema("ha_service", "Call ANY Home Assistant service for advanced control (scenes, scripts, climate, media). E.g. domain='scene', service='turn_on', entity_id='scene.movie_night'.", {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Service domain, e.g. scene, script, climate, media_player."},
                    "service": {"type": "string", "description": "Service name, e.g. turn_on, set_temperature, volume_set."},
                    "entity_id": {"type": "string", "description": "Target entity id (optional for some services)."},
                    "data": {"type": "object", "description": "Optional extra service data (merged with entity_id)."},
                },
                "required": ["domain", "service"],
            }), self._service, authority="owner"),
        ]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

    async def _states(self, force: bool = False) -> list[dict]:
        if not force and self._states_cache and (time.time() - self._states_ts) < _CACHE_TTL:
            return self._states_cache
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{BASE_URL}/api/states", headers=self._headers())
            r.raise_for_status()
            self._states_cache = r.json()
            self._states_ts = time.time()
        return self._states_cache

    async def _list(self, a: dict) -> dict:
        domain = str(a.get("domain", "")).strip()
        search = str(a.get("search", "")).strip().lower()
        try:
            states = await self._states()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        ents = []
        for s in states:
            eid = s.get("entity_id", "")
            name = s.get("attributes", {}).get("friendly_name", eid)
            if domain and not eid.startswith(domain + "."):
                continue
            if search and search not in eid.lower() and search not in str(name).lower():
                continue
            ents.append({"entity_id": eid, "state": s.get("state"), "name": name})
        return {"ok": True, "count": len(ents), "entities": ents[:60]}

    async def _get_state(self, a: dict) -> dict:
        eid = str(a.get("entity_id", "")).strip()
        if "." not in eid:
            return {"ok": False, "error": "entity_id must look like 'light.kitchen'"}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{BASE_URL}/api/states/{eid}", headers=self._headers())
                r.raise_for_status()
                s = r.json()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        attrs = s.get("attributes", {})
        # Keep a few useful attributes, not the whole blob.
        keep = {k: attrs[k] for k in ("friendly_name", "brightness", "temperature", "current_temperature",
                                      "device_class", "unit_of_measurement") if k in attrs}
        return {"ok": True, "entity_id": eid, "state": s.get("state"), "attributes": keep}

    async def _post_service(self, domain: str, service: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{BASE_URL}/api/services/{domain}/{service}",
                             headers=self._headers(), json=payload)
            r.raise_for_status()
        self._states_ts = 0.0   # state likely changed; invalidate the cache
        return {"ok": True}

    async def _call(self, a: dict) -> dict:
        eid = str(a.get("entity_id", "")).strip()
        action = str(a.get("action", "toggle")).strip()
        if "." not in eid:
            return {"ok": False, "error": "entity_id must look like 'light.kitchen'"}
        domain = eid.split(".", 1)[0]
        data = a.get("data") if isinstance(a.get("data"), dict) else {}
        payload = {"entity_id": eid, **data}
        try:
            # <domain>.turn_on/off/toggle accepts service data (brightness, color, ...); fall back to the
            # generic homeassistant.* service if the domain doesn't define it.
            try:
                await self._post_service(domain, action, payload)
            except Exception:  # noqa: BLE001
                await self._post_service("homeassistant", action, payload)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "did": f"{action} {eid}" + (f" {data}" if data else "")}

    async def _service(self, a: dict) -> dict:
        domain = str(a.get("domain", "")).strip()
        service = str(a.get("service", "")).strip()
        if not domain or not service:
            return {"ok": False, "error": "domain and service required (e.g. scene/turn_on)"}
        payload = dict(a.get("data") if isinstance(a.get("data"), dict) else {})
        eid = str(a.get("entity_id", "")).strip()
        if eid:
            payload["entity_id"] = eid
        try:
            await self._post_service(domain, service, payload)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "did": f"{domain}.{service} {payload or ''}".strip()}
