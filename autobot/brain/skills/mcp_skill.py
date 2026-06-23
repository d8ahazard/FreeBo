"""MCP skill — expose tools from external Model Context Protocol servers to the cortex.

This lets FreeBo's brain use any MCP server (Home Assistant, filesystem, web search, your own tools, ...) as
first-class tools, alongside its built-in skills. It only works on the tool-calling cortex path (the `vlm`
brain has no tools). Everything is optional and fail-soft: if the `mcp` package isn't installed or no servers
are configured, the skill simply contributes nothing.

Config: set `AUTOBOT_MCP_SERVERS` to a JSON object mapping a short server id to a connection spec:

  {
    "home_assistant": {"url": "http://homeassistant.local:8123/mcp_server/sse",
                        "headers": {"Authorization": "Bearer <long-lived-token>"}, "authority": "owner"},
    "files":          {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
                        "authority": "owner", "allow": ["read_file", "list_directory"]}
  }

  - `url`  -> SSE/HTTP transport (set "transport":"http" for streamable HTTP). `command`/`args`/`env` -> stdio.
  - `authority`: "owner" (default — external tools can have real side effects) or "anyone".
  - `allow`: optional allowlist of tool names to expose from that server.

Design: all MCP I/O runs on ONE dedicated background event loop (its own thread) so we never fight the main
app loop over anyio task-group ownership. The main loop bridges calls via run_coroutine_threadsafe. Tool
discovery happens once at startup; each call opens a short-lived session (robust over long uptimes). See
docs/AI_BRAIN.md.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time

from .base import Skill, SkillContext, ToolDef, fn_schema


def _mcp_available() -> bool:
    try:
        import mcp  # type: ignore  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_config() -> dict:
    raw = os.environ.get("AUTOBOT_MCP_SERVERS", "").strip()
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:  # noqa: BLE001
        print("[mcp] AUTOBOT_MCP_SERVERS is not valid JSON — ignoring", flush=True)
        return {}


def _safe_name(server: str, tool: str) -> str:
    """A function-call-safe exposed tool name like `mcp_home_assistant_turn_on`."""
    s = re.sub(r"[^a-zA-Z0-9_]", "_", f"mcp_{server}_{tool}")
    return s[:64]


class McpManager:
    """Owns a private event loop on a daemon thread; connects to MCP servers, lists tools, and runs calls."""

    def __init__(self, config: dict):
        self.config = config
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        # exposed tool name -> {"server", "tool", "schema", "authority"}
        self.tools: dict[str, dict] = {}
        self.errors: dict[str, str] = {}

    # --- lifecycle (runs on the dedicated thread) ---
    def run(self) -> None:
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._discover_all())
            self._ready.set()
            self.loop.run_forever()
        except Exception as e:  # noqa: BLE001 - never crash the host app
            print(f"[mcp] manager loop failed: {e}", flush=True)
            self._ready.set()

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self.run, name="mcp-manager", daemon=True)
        self._thread.start()

    # --- transport ---
    def _session(self, cfg: dict):
        """Async context manager yielding an initialized ClientSession for a server spec."""
        from contextlib import asynccontextmanager

        from mcp import ClientSession  # type: ignore

        @asynccontextmanager
        async def _ctx():
            if cfg.get("command"):
                from mcp import StdioServerParameters  # type: ignore
                from mcp.client.stdio import stdio_client  # type: ignore
                params = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []),
                                               env={**os.environ, **(cfg.get("env") or {})})
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
            elif cfg.get("url"):
                headers = cfg.get("headers") or {}
                if str(cfg.get("transport", "")).lower() == "http":
                    from mcp.client.streamable_http import streamablehttp_client  # type: ignore
                    async with streamablehttp_client(cfg["url"], headers=headers) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            yield session
                else:
                    from mcp.client.sse import sse_client  # type: ignore
                    async with sse_client(cfg["url"], headers=headers) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            yield session
            else:
                raise ValueError("server spec needs 'command' (stdio) or 'url' (sse/http)")

        return _ctx()

    async def _discover_all(self) -> None:
        for server, cfg in self.config.items():
            if not isinstance(cfg, dict):
                continue
            try:
                async with self._session(cfg) as session:
                    listed = await session.list_tools()
                allow = set(cfg.get("allow") or [])
                authority = "anyone" if str(cfg.get("authority", "owner")).lower() == "anyone" else "owner"
                count = 0
                for t in listed.tools:
                    if allow and t.name not in allow:
                        continue
                    exposed = _safe_name(server, t.name)
                    schema = fn_schema(exposed, (t.description or f"{server}: {t.name}")[:1024],
                                       t.inputSchema or {"type": "object", "properties": {}})
                    self.tools[exposed] = {"server": server, "tool": t.name, "schema": schema,
                                           "authority": authority}
                    count += 1
                print(f"[mcp] {server}: {count} tools", flush=True)
            except Exception as e:  # noqa: BLE001
                self.errors[server] = f"{type(e).__name__}: {e}"
                print(f"[mcp] {server} connect failed: {e}", flush=True)

    async def _call(self, server: str, tool: str, args: dict) -> dict:
        cfg = self.config.get(server) or {}
        try:
            async with self._session(cfg) as session:
                res = await session.call_tool(tool, args or {})
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # Flatten content blocks into text for the LLM.
        chunks: list[str] = []
        for block in getattr(res, "content", []) or []:
            txt = getattr(block, "text", None)
            chunks.append(txt if txt is not None else str(block))
        is_error = bool(getattr(res, "isError", False))
        return {"ok": not is_error, "result": "\n".join(chunks).strip() or None,
                "server": server, "tool": tool}

    def call_from_loop(self, server: str, tool: str, args: dict, timeout: float = 30.0):
        """Schedule a call on the manager loop; returns a concurrent.futures.Future (await via wrap_future)."""
        if not self.loop:
            raise RuntimeError("mcp manager not started")
        return asyncio.run_coroutine_threadsafe(self._call(server, tool, args), self.loop)


class McpSkill(Skill):
    name = "mcp"

    def __init__(self):
        self._config = _load_config()
        self._ok = bool(self._config) and _mcp_available()
        self._mgr: McpManager | None = McpManager(self._config) if self._ok else None
        if not self._config:
            self._reason = "AUTOBOT_MCP_SERVERS not set"
        elif not _mcp_available():
            self._reason = "the `mcp` package isn't installed (pip install mcp)"
        else:
            self._reason = ""

    def available(self, ctx: SkillContext) -> tuple[bool, str]:
        return (self._ok, self._reason)

    def background_workers(self, ctx: SkillContext):
        # Start the manager's dedicated event loop + discover tools once, at app startup.
        if self._mgr:
            return [self._mgr.start]
        return []

    def system_prompt_fragment(self, ctx: SkillContext) -> str:
        if not self._mgr or not self._mgr.tools:
            return ""
        servers = sorted({d["server"] for d in self._mgr.tools.values()})
        return (f"EXTERNAL TOOLS (MCP): you also have tools from {', '.join(servers)} (names start with "
                "`mcp_`). Use them to act beyond your body — e.g. control the smart home, look things up, or "
                "run helpers. Read each tool's description; call them like any other tool.")

    def tools(self, ctx: SkillContext) -> list[ToolDef]:
        if not self._mgr:
            return []
        out: list[ToolDef] = []
        for exposed, meta in list(self._mgr.tools.items()):
            out.append(ToolDef(meta["schema"], self._make_handler(meta["server"], meta["tool"]),
                               authority=meta["authority"]))
        return out

    def _make_handler(self, server: str, tool: str):
        async def h(a: dict) -> dict:
            mgr = self._mgr
            if not mgr or not mgr.loop:
                return {"ok": False, "error": "mcp manager not ready"}
            try:
                fut = mgr.call_from_loop(server, tool, a or {})
                return await asyncio.wrap_future(fut)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return h
