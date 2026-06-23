"""A thin async HTTP client for the running Autobot app — the only thing the self-test talks to.

Everything here is read/command over the same REST surface the web UI uses, so the harness exercises the
exact LINK + brain the app is running (no second Agora session). httpx is already an app dependency.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx


class AppClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8200", timeout: float = 20.0) -> None:
        self.base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AppClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # --- raw helpers ---
    async def _get_json(self, path: str) -> dict:
        r = await self._http.get(self.base + path)
        r.raise_for_status()
        return r.json()

    async def _post_json(self, path: str, body: Optional[dict] = None) -> dict:
        r = await self._http.post(self.base + path, json=body or {})
        r.raise_for_status()
        return r.json()

    # --- state / settings ---
    async def state(self) -> dict:
        return await self._get_json("/api/state")

    async def settings(self, **changes) -> dict:
        return await self._post_json("/api/settings", changes)

    async def telemetry(self) -> dict:
        """Live telemetry via the diagnostics-friendly REST endpoint (added alongside this harness)."""
        return await self._get_json("/api/telemetry")

    async def heard(self) -> list[dict]:
        """Recent transcripts the brain has heard (for the audio-in check)."""
        d = await self._get_json("/api/diag/heard")
        return d.get("heard", []) if isinstance(d, dict) else []

    async def slam_map(self) -> dict:
        return await self._get_json("/api/slam/map")

    # --- control ---
    async def control(self, **body) -> dict:
        return await self._post_json("/api/control", body)

    async def stop(self) -> dict:
        return await self.control(kind="stop")

    async def estop(self) -> dict:
        return await self._post_json("/api/estop")

    async def tick(self) -> dict:
        return await self._post_json("/api/tick")

    # --- media ---
    async def snapshot(self) -> tuple[Optional[bytes], Optional[str]]:
        """(jpeg_bytes, error). The app returns 503 + an X-Reason header when no frame is available."""
        r = await self._http.get(self.base + "/api/snapshot.jpg")
        if r.status_code == 200 and r.content:
            return r.content, None
        return None, r.headers.get("X-Reason", f"http {r.status_code}")

    async def voice_say(self, text: str) -> tuple[bool, str]:
        """Render TTS to WAV server-side (proves the TTS pipeline works even if robot talkback is stubbed)."""
        r = await self._http.get(self.base + "/api/voice/say", params={"text": text})
        if r.status_code == 200 and r.content:
            return True, f"{len(r.content)} bytes wav"
        return False, r.headers.get("X-Reason", f"http {r.status_code}")

    # --- misc ---
    async def ping(self) -> bool:
        try:
            await self.state()
            return True
        except Exception:  # noqa: BLE001
            return False
