"""Optional local embedding backend for semantic memory recall.

Opt-in: set `AUTOBOT_EMBED_MODEL` (e.g. `nomic-embed-text` on Ollama). It talks to an OpenAI-compatible
`/embeddings` endpoint (defaults to the same base URL/key as the cortex model), so on the recommended local
stack it just works against the same Ollama server. Everything is fail-soft: if it's not configured or the
call fails, `embed()` returns None and callers fall back to keyword recall. Stdlib + httpx only (a web dep),
keeping `autobot/brain` Pi-friendly. See docs/AI_BRAIN.md.
"""
from __future__ import annotations

import math
import os

import httpx


def embed_model() -> str:
    return os.environ.get("AUTOBOT_EMBED_MODEL", "")


def _base_url() -> str:
    return (os.environ.get("AUTOBOT_EMBED_BASE_URL") or os.environ.get("AUTOBOT_AI_BASE_URL", "")).rstrip("/")


def _api_key() -> str:
    return os.environ.get("AUTOBOT_EMBED_API_KEY") or os.environ.get("AUTOBOT_AI_API_KEY", "")


def embeddings_enabled() -> bool:
    return bool(embed_model() and _base_url())


def embed(texts: list[str], timeout: float = 10.0) -> list[list[float]] | None:
    """Embed a batch of texts. Returns one vector per input, or None if disabled/failed (fail-soft)."""
    if not embeddings_enabled() or not texts:
        return None
    headers = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(_base_url() + "/embeddings",
                       json={"model": embed_model(), "input": texts}, headers=headers)
            r.raise_for_status()
            data = r.json().get("data", [])
        vecs = [d.get("embedding") for d in data if isinstance(d, dict)]
        return vecs if len(vecs) == len(texts) else None
    except Exception:  # noqa: BLE001 - recall must never break on an embedding hiccup
        return None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
