"""A small catalog of OpenAI-compatible providers for the setup wizard.

For each provider we suggest a FAST model (used every interaction tick) and a HEAVY model (used once a day
for memory cleanup/summarization). These are starting points the user can override; they're intentionally
vision-capable where possible since the robot's perception is image-based. Nothing here is authoritative —
model names change; treat suggestions as defaults, not guarantees.
"""
from __future__ import annotations

PROVIDERS: list[dict] = [
    {
        "key": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "needs_key": True,
        "fast": "gpt-4o-mini",
        "heavy": "gpt-4o",
        "notes": "Reliable vision + tool calling. Cheapest fast option is gpt-4o-mini.",
    },
    {
        "key": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "needs_key": True,
        "fast": "openai/gpt-4o-mini",
        "heavy": "anthropic/claude-3.5-sonnet",
        "notes": "One key, many models. Pick any vision+tools model for fast; a stronger one for daily memory.",
    },
    {
        "key": "ollama",
        "name": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
        "needs_key": False,
        "fast": "llama3.2-vision",
        "heavy": "qwen2.5:14b",
        "notes": "Runs on your own machine, no API key. Needs a vision model for the camera (e.g. llama3.2-vision).",
    },
    {
        "key": "lmstudio",
        "name": "LM Studio (local)",
        "base_url": "http://localhost:1234/v1",
        "needs_key": False,
        "fast": "qwen2.5-vl-7b",
        "heavy": "qwen2.5-vl-32b",
        "notes": "Local OpenAI-compatible server. Load a vision model for perception.",
    },
    {
        "key": "xai",
        "name": "xAI (Grok)",
        "base_url": "https://api.x.ai/v1",
        "needs_key": True,
        "fast": "grok-4",
        "heavy": "grok-4",
        "notes": "Grok over the OpenAI-compatible xAI API. grok-4 is multimodal (handles the camera). Needs "
                 "team credits at console.x.ai. Swap fast to a cheaper model once you confirm it works.",
    },
    {
        "key": "groq",
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "needs_key": True,
        "fast": "llama-3.2-11b-vision-preview",
        "heavy": "llama-3.3-70b-versatile",
        "notes": "Very fast inference. Use a vision model for fast; a 70b for daily summarization.",
    },
    {
        "key": "custom",
        "name": "Custom (any OpenAI-compatible endpoint)",
        "base_url": "",
        "needs_key": False,
        "fast": "",
        "heavy": "",
        "notes": "Enter your own base URL + models (vLLM, a proxy, a self-hosted gateway, etc.).",
    },
]

_BY_KEY = {p["key"]: p for p in PROVIDERS}


def get_provider(key: str) -> dict | None:
    return _BY_KEY.get(key)


def catalog_for_ui() -> list[dict]:
    """Provider catalog for the setup wizard (no secrets; just suggestions)."""
    return PROVIDERS
