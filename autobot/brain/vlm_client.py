"""Client for the light vision service (scripts/vlm_service.py, moondream2).

Part of FreeBo's modular brain: this fetches a navigation decision + a short spoken line from the vision
service. Hearing (faster-whisper) and speech (Piper) live in the main app, so this is purely the "see +
decide" piece.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx


def vlm_base_url() -> str:
    return os.environ.get("AUTOBOT_VLM_URL", "http://127.0.0.1:8360").rstrip("/")


def hybrid_enabled() -> bool:
    """Reflex+cortex brain: the VLM is the SENSES (it perceives the scene) and a separate tool-calling LLM
    (the cortex) does the thinking/acting. Activated with AUTOBOT_AI_PROVIDER=hybrid. See docs/AI_BRAIN.md."""
    return os.environ.get("AUTOBOT_AI_PROVIDER", "") == "hybrid"


def vlm_perception_enabled() -> bool:
    """True when the VLM should run purely as a perception/caption tier feeding the cortex (hybrid mode)."""
    return hybrid_enabled() and bool(vlm_base_url())


def vlm_enabled() -> bool:
    """True when the VLM is the WHOLE brain (it both sees and decides the move). Disabled in hybrid mode,
    where the VLM only perceives and the tool-calling cortex decides."""
    if hybrid_enabled():
        return False
    return bool(os.environ.get("AUTOBOT_VLM_URL")) or os.environ.get("AUTOBOT_AI_PROVIDER", "") == "vlm"


class VlmError(RuntimeError):
    pass


class VlmClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0) -> None:
        self.base_url = (base_url or vlm_base_url()).rstrip("/")
        self.timeout = timeout

    async def decide(self, *, frames_b64: Optional[list[str]] = None, mode: str = "explore",
                     heard: str = "", language: str = "en", describe: bool = False,
                     persona: str = "", robot_name: str = "FreeBo", directive: str = "") -> dict:
        payload = {"frames_b64": frames_b64 or [], "mode": mode, "heard": heard,
                   "language": language, "describe": describe, "persona": persona,
                   "robot_name": robot_name, "directive": directive}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.base_url + "/vlm/decide", json=payload)
                r.raise_for_status()
                return r.json()
        except Exception as e:  # noqa: BLE001
            raise VlmError(f"{type(e).__name__}: {e}") from e

    async def perceive(self, *, frames_b64: Optional[list[str]] = None, robot_name: str = "FreeBo",
                       persona: str = "") -> dict:
        """Hybrid-brain perception: a concise, navigation+companion oriented description of the scene that
        the tool-calling cortex reads as its eyes. Returns {ok, text}. The VLM does NOT decide a move here."""
        payload = {"frames_b64": frames_b64 or [], "robot_name": robot_name, "persona": persona}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.base_url + "/vlm/perceive", json=payload)
                r.raise_for_status()
                return r.json()
        except Exception as e:  # noqa: BLE001
            raise VlmError(f"{type(e).__name__}: {e}") from e
