"""Client for the MiniCPM-o 2.6 omni brain service (scripts/omni_service.py).

The omni model is FreeBo's real brain: it sees the live camera frame(s), (optionally) hears audio, and
replies with natural TEXT + native SPEECH audio in one pass — no separate vision/caption/TTS models. This
thin async client posts a turn to the service's /omni/respond endpoint and returns its reply.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx


def omni_base_url() -> str:
    return os.environ.get("AUTOBOT_OMNI_URL", "http://127.0.0.1:8350").rstrip("/")


def omni_enabled() -> bool:
    return bool(os.environ.get("AUTOBOT_OMNI_URL")) or os.environ.get("AUTOBOT_AI_PROVIDER", "") == "omni"


class OmniError(RuntimeError):
    pass


class OmniClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 60.0) -> None:
        self.base_url = (base_url or omni_base_url()).rstrip("/")
        self.timeout = timeout

    async def healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(self.base_url + "/health")
                return bool(r.json().get("ok"))
        except Exception:  # noqa: BLE001
            return False

    async def respond(self, *, frames_b64: Optional[list[str]] = None, audio_b64: Optional[str] = None,
                      instruction: Optional[str] = None, language: str = "en") -> dict:
        """One omni turn -> {ok, text, speech_b64, sr}. Raises OmniError on transport failure."""
        payload = {"frames_b64": frames_b64 or [], "audio_b64": audio_b64,
                   "instruction": instruction, "language": language}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.base_url + "/omni/respond", json=payload)
                r.raise_for_status()
                return r.json()
        except Exception as e:  # noqa: BLE001
            raise OmniError(f"{type(e).__name__}: {e}") from e
