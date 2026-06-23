"""Cloud RTM transport (Phase C) — carries eboproto's RTM JSON to Air 2 / EBO Max class robots.

Higher-end Enabot models (Air 2, EBO Max) drive motion over the app's cloud **RTM** (realtime-messaging)
channel rather than LAN MAVLink. `autobot/robot/proto.py` already BUILDS those JSON commands and
`proto.route()` knows which actions go to RTM; this module is the missing piece: the authenticated cloud
websocket that actually delivers them.

    STATUS: NOT IMPLEMENTED (scaffold). Building this requires Enabot account auth + the cloud RTM endpoint
    handshake (peer like `air2_us_ebox-prod_<id>`, REST login at *.enabotserverintl.com). It is intentionally
    deferred — see docs/TODO-FREEBO.md §5.

    LOCAL-FIRST TRADEOFF: the EBO **SE** is fully local (LAN MAVLink, no cloud). Air 2 / EBO Max motion
    control rides Enabot's cloud relay, so those models are NOT 100% offline. We surface that honestly rather
    than pretend otherwise.

Until implemented, `RTMTransport.connected` stays False and `send_json()` returns not-connected, so a link
that routes an action to RTM degrades gracefully (the UI/AI sees it's unsupported on this build).
"""
from __future__ import annotations

from typing import Any


class RTMTransport:
    def __init__(self, settings: Any | None = None):
        self.settings = settings
        self.connected = False
        self.sid: str | None = None
        self._reason = "RTM transport not implemented (Phase C — Air 2 / EBO Max cloud relay)"

    async def connect(self) -> dict[str, Any]:
        # TODO(Phase C): REST login -> obtain session + RTM endpoint -> open authenticated websocket ->
        # join the device peer. See ebo-se-lan-bridge/vendor/lib/_re/PROTOCOL_NOTES.md "App RTM control".
        return {"ok": False, "connected": False, "error": self._reason}

    async def send_json(self, payload: str) -> dict[str, Any]:
        """payload is a JSON string from proto.rtm_*() / proto via ebo_build (channel == rtm_json)."""
        if not self.connected:
            return {"ok": False, "error": self._reason}
        return {"ok": False, "error": "unreachable"}  # pragma: no cover

    async def close(self) -> None:
        self.connected = False
