"""Overseer puppet mode — a RobotLink wrapper that paralyzes the AI brain.

When `settings.overseer` is on, the brain keeps perceiving, thinking, and emitting tool calls, but every
robot-affecting verb it issues (drive/move/say/action/...) is INTERCEPTED here: it is recorded as a
*proposal* (so a human/agent overseer can see what the dumb brain wanted to do) and a synthetic `ok=True`
is returned so the brain believes it succeeded. Nothing reaches the real robot.

The overseer drives the real robot separately, through the web server's `/api/overseer/act` endpoint, which
talks to the *unwrapped* link. This is the single chokepoint for "paralysis": the brain only ever touches
the robot through a RobotLink (see docs/ARCHITECTURE.md), so wrapping that link is enough.

Read-only verbs (info/telemetry/snapshot/connection) and inputs (audio sink) always pass through, so the
overseer still sees exactly what the brain sees. `stop` also passes through (stopping is always safe and
keeps the robot halted between overseer commands), but is still recorded as intent.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .link import RobotLink


@dataclass
class Proposal:
    """One robot-affecting action the (paralyzed) brain tried to take."""
    seq: int
    ts: float
    verb: str
    args: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "verb": self.verb, "args": self.args}


class ProposalStore:
    """Thread-safe ring buffer of intercepted brain intents, with a monotonic cursor so the overseer can
    poll 'what has the brain tried since cursor N'."""

    def __init__(self, maxlen: int = 200) -> None:
        self._items: deque[Proposal] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, verb: str, args: dict | None = None) -> Proposal:
        with self._lock:
            self._seq += 1
            p = Proposal(self._seq, time.time(), verb, dict(args or {}))
            self._items.append(p)
            return p

    def since(self, cursor: int) -> tuple[list[Proposal], int]:
        """Proposals with seq > cursor, plus the latest seq (the new cursor)."""
        with self._lock:
            items = [p for p in self._items if p.seq > cursor]
            return items, self._seq

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq


EmitFn = Callable[[dict], Awaitable[None]]


class OverseerGate(RobotLink):
    """Wraps a real RobotLink. Effect verbs are intercepted while `settings.overseer` is on; everything else
    is delegated to the wrapped link unchanged."""

    def __init__(self, inner: RobotLink, settings, store: ProposalStore, emit: EmitFn | None = None) -> None:
        self._inner = inner
        self._settings = settings
        self._store = store
        self._emit = emit
        # Mirror the wrapped link's variant so control routing/heuristics see the real robot.
        try:
            self.variant = getattr(inner, "variant", "SE")
        except Exception:  # noqa: BLE001
            pass
        # Only advertise publish_speech when the wrapped link actually supports it (Air 2 RTC). The brain
        # probes for it with getattr(link, "publish_speech", None); exposing it unconditionally would break
        # the say path on links that speak via local G.711.
        if callable(getattr(inner, "publish_speech", None)):
            self.publish_speech = self._publish_speech  # type: ignore[assignment]

    # --- overseer state ---
    def _on(self) -> bool:
        try:
            return bool(self._settings.snapshot().overseer)
        except Exception:  # noqa: BLE001
            return False

    async def _intercept(self, verb: str, args: dict) -> dict:
        p = self._store.add(verb, args)
        if self._emit is not None:
            try:
                await self._emit({"type": "proposal", "seq": p.seq, "ts": p.ts,
                                  "verb": verb, "args": p.args})
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "intercepted": True, "verb": verb, "seq": p.seq}

    # --- read / passthrough ---
    async def info(self) -> dict[str, Any]:
        return await self._inner.info()

    async def telemetry(self) -> dict[str, Any]:
        return await self._inner.telemetry()

    async def snapshot(self) -> tuple[bytes | None, str | None]:
        return await self._inner.snapshot()

    async def connection(self, state: str) -> dict[str, Any]:
        return await self._inner.connection(state)

    # --- effect verbs (intercepted while overseer is on) ---
    async def drive(self, ly: float, rx: float, *, generation: int | None = None,
                    epoch: int | None = None, ticket_id: int | None = None) -> dict[str, Any]:
        if self._on():
            return await self._intercept("drive", {"ly": ly, "rx": rx})
        return await self._inner.drive(ly, rx, generation=generation, epoch=epoch, ticket_id=ticket_id)

    async def move(self, ly: float, rx: float, duration: float, *, generation: int | None = None,
                   epoch: int | None = None, ticket_id: int | None = None) -> dict[str, Any]:
        if self._on():
            return await self._intercept("move", {"ly": ly, "rx": rx, "duration": duration})
        return await self._inner.move(ly, rx, duration, generation=generation, epoch=epoch, ticket_id=ticket_id)

    async def stop(self) -> dict[str, Any]:
        # Always let stop through (safe + keeps the robot halted between overseer commands); record intent.
        if self._on():
            self._store.add("stop", {})
        return await self._inner.stop()

    # --- safety ops: ALWAYS delegate to the real link, NEVER intercept as a proposal (P0-R4.1/fix). The base
    # RobotLink.estop() degrades to stop(); without these explicit overrides the gate would inherit that
    # degraded path and a master STOP would never reach Air2NativeLink.estop()'s latch + zero-frame burst.
    async def estop(self, generation: int | None = None, epoch: int | None = None) -> dict[str, Any]:
        return await self._inner.estop(generation=generation, epoch=epoch)

    async def estop_reset(self, *, expected_epoch: int | None = None, expected_generation: int | None = None,
                          release_epoch: int | None = None,
                          release_generation: int | None = None) -> dict[str, Any]:
        return await self._inner.estop_reset(expected_epoch=expected_epoch, expected_generation=expected_generation,
                                             release_epoch=release_epoch, release_generation=release_generation)

    async def action(self, name: str, *, source: str = "ai") -> dict[str, Any]:
        if self._on():
            return await self._intercept("action", {"name": name})
        return await self._inner.action(name, source=source)

    # ticketed effect dispatch + admitter wiring pass-throughs (agent_next_2 §4)
    def set_effect_admitter(self, admit, settings_getter) -> None:
        fn = getattr(self._inner, "set_effect_admitter", None)
        if callable(fn):
            fn(admit, settings_getter)

    async def set_move_mode(self, mode: int, *, source: str = "ai") -> dict[str, Any]:
        fn = getattr(self._inner, "set_move_mode", None)
        return await fn(mode, source=source) if callable(fn) else {"ok": False, "error": "unsupported"}

    async def set_move_speed(self, speed: int, *, source: str = "ai") -> dict[str, Any]:
        fn = getattr(self._inner, "set_move_speed", None)
        return await fn(speed, source=source) if callable(fn) else {"ok": False, "error": "unsupported"}

    async def say_audio(self, g711: bytes, codec: str = "mulaw") -> dict[str, Any]:
        if self._on():
            return await self._intercept("say_audio", {"codec": codec, "bytes": len(g711 or b"")})
        return await self._inner.say_audio(g711, codec)

    async def say_text(self, text: str) -> dict[str, Any]:
        if self._on():
            return await self._intercept("say_text", {"text": text})
        return await self._inner.say_text(text)

    async def _publish_speech(self, wav: bytes) -> dict[str, Any]:
        if self._on():
            return await self._intercept("publish_speech", {"bytes": len(wav or b"")})
        return await self._inner.publish_speech(wav)  # type: ignore[attr-defined]

    # --- capabilities / inputs (delegate) ---
    def prefers_text_tts(self) -> bool:
        return self._inner.prefers_text_tts()

    def set_audio_sink(self, callback) -> None:
        return self._inner.set_audio_sink(callback)

    @property
    def whep_upstream(self) -> str | None:
        return self._inner.whep_upstream

    @property
    def hls_base(self) -> str | None:
        return self._inner.hls_base

    def stream_auth_header(self) -> dict[str, str]:
        return self._inner.stream_auth_header()

    def __getattr__(self, name: str) -> Any:
        # Delegate anything not defined above (e.g. hub, send_rdt, rtm) to the wrapped link. Only invoked
        # when normal attribute lookup fails, so it never shadows the methods/properties defined here.
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)
