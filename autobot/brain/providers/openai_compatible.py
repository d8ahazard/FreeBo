"""A minimal, vendor-neutral client for OpenAI-compatible Chat Completions endpoints.

We deliberately speak plain HTTP (no vendor SDK) so any compatible server works: OpenAI, Ollama, LM Studio,
OpenRouter, vLLM, a Gemini/Anthropic OpenAI-compat shim, etc. We assume only:
  - POST {base_url}/chat/completions
  - `tools` + `tool_choice` (function calling) — optional; we degrade if unsupported
  - image content parts (data URLs) — optional; we degrade to text-only if the model lacks vision

See ../../../docs/AI_BRAIN.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


class ProviderError(Exception):
    pass


@dataclass
class ChatResult:
    content: str                       # the assistant's natural-language "thoughts"
    tool_calls: list[dict] = field(default_factory=list)   # [{id, name, arguments(dict)}]
    raw: dict | None = None
    finish_reason: str | None = None


class OpenAICompatibleClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float = 0.4) -> ChatResult:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        url = f"{self.base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(url, headers=headers, json=payload)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"request failed: {type(e).__name__}: {e}") from e

        if r.status_code >= 400:
            raise ProviderError(_format_http_error(r))

        try:
            data = r.json()
            choice = data["choices"][0]
            msg = choice.get("message", {})
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"bad response shape: {type(e).__name__}: {e}") from e

        content = msg.get("content") or ""
        if isinstance(content, list):  # some servers return content as parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))

        tool_calls = _parse_tool_calls(msg)
        return ChatResult(content=content, tool_calls=tool_calls, raw=data,
                          finish_reason=choice.get("finish_reason"))


def _parse_tool_calls(msg: dict) -> list[dict]:
    import json
    out: list[dict] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {"_raw": args}
        out.append({"id": tc.get("id", ""), "name": fn.get("name", ""), "arguments": args or {}})
    return out


def _format_http_error(r: httpx.Response) -> str:
    try:
        body = r.json()
        msg = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
        detail = msg or str(body)[:300]
    except Exception:  # noqa: BLE001
        detail = (r.text or "")[:300]
    return f"HTTP {r.status_code}: {detail}"
