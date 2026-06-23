"""Optional "smart supervises dumb" layer.

The robot's reliable protection is deterministic (the cerebellum's per-step camera confirmation + the vision
reflex + the safety floor) — none of which need a second GPU model, which is the whole point under our
portability budget. This module is an OPTIONAL extra: when `config.supervisor` is on, a (usually stronger /
cloud) vision model vets the camera right before a FORWARD step and can veto it if the path isn't clearly
open. It is OFF by default; pointed at a cloud endpoint it costs zero local VRAM.

Provider-agnostic (OpenAI-compatible). Fail-safe-by-availability: if the supervisor errors/times out it
ALLOWS the step (the deterministic layers still protect the robot) rather than freezing the robot.
"""
from __future__ import annotations

import base64

from ..config import Settings
from .providers import OpenAICompatibleClient, ProviderError

_PROMPT = (
    "You are the safety supervisor for a small floor robot about to take ONE short FORWARD step. Look at its "
    "camera frame. Is the floor directly ahead clearly open and safe to roll forward ~half a meter — no wall, "
    "furniture, person, pet, cable, drop-off/stairs, or clutter close ahead? Answer with ONLY one word: "
    "SAFE or BLOCK.")


def enabled(s: Settings) -> bool:
    if not getattr(s, "supervisor", False):
        return False
    # Need somewhere to call. A local vision brain (vlm/omni) has no chat endpoint here, so require an
    # OpenAI-compatible base_url (cloud or local text/vision server) + a model.
    return bool(s.ai_base_url and (s.ai_supervisor_model or s.ai_model))


async def vet_step(jpeg: bytes, s: Settings) -> tuple[bool, str]:
    """Return (allow, reason). Allows on any error so the robot never deadlocks on a flaky supervisor."""
    if not jpeg:
        return True, "no frame to vet"
    model = s.ai_supervisor_model or s.ai_model
    client = OpenAICompatibleClient(s.ai_base_url, s.ai_api_key, model, timeout=12.0)
    url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": _PROMPT},
        {"type": "image_url", "image_url": {"url": url}},
    ]}]
    try:
        res = await client.chat(messages, temperature=0.0)
        word = (res.content or "").strip().upper()
        if word.startswith("BLOCK"):
            return False, "supervisor: path not clear ahead"
        return True, "supervisor: clear"
    except ProviderError as e:
        return True, f"supervisor unavailable ({e}) — allowed"
    except Exception as e:  # noqa: BLE001
        return True, f"supervisor error ({type(e).__name__}) — allowed"
