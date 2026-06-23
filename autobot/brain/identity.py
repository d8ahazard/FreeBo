"""Identity + owner authority — the "pairing" / obedience layer.

This answers "who is the robot talking to, and may they command it?" It is deliberately pluggable:

  - The recognition skill (face recognition) calls `set_present()` as it identifies people on camera.
  - If no recognizer is running, the dashboard is assumed to be the owner (single-user dev stays usable).

When `obey_owner_only` is ON, physical/command tools require the owner to be present (recognized), the
dashboard owner, or a live approval window. Otherwise the robot "asks its maker": it creates a pending
approval the owner resolves from the UI (or by voice), which opens a short window during which gated
commands pass. This is enforced mechanically alongside the safety floor — it is not prompt trust.

Pairing ("I am your maker") = enroll the owner's face via the recognition skill, so the robot can recognize
the owner on sight thereafter.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..config import Settings

PRESENCE_TTL = 30.0          # a recognized person is "present" for this long after last seen (s)
APPROVAL_WINDOW = 120.0      # how long an owner approval lets gated commands through (s)
EmitFn = Callable[[dict], Awaitable[None]]


@dataclass
class Pending:
    id: str
    tool: str
    args: dict
    requester: str
    reason: str
    ts: float = field(default_factory=time.time)


class Identity:
    def __init__(self, emit: EmitFn | None = None):
        self.emit = emit
        self._lock = threading.RLock()
        self._present: dict[str, float] = {}        # name -> last-seen monotonic-ish (time.time)
        self._approval_until = 0.0                   # owner-granted "obey others" window
        self._pending: dict[str, Pending] = {}
        self._recognizer_active = False              # set True when the recognition skill is running
        self._seq = 0

    # --- presence (updated by the recognition skill) ---
    def set_recognizer_active(self, active: bool):
        self._recognizer_active = active

    def set_present(self, names: list[str]):
        now = time.time()
        with self._lock:
            for n in names:
                if n:
                    self._present[n] = now

    def present_people(self) -> list[str]:
        now = time.time()
        with self._lock:
            return [n for n, ts in self._present.items() if now - ts < PRESENCE_TTL]

    def is_owner(self, name: str, s: Settings) -> bool:
        return bool(s.owner_name) and name.strip().lower() == s.owner_name.strip().lower()

    def owner_present(self, s: Settings) -> bool:
        # If no recognizer is running, we can't see faces -> trust the dashboard as the owner.
        if not self._recognizer_active:
            return True
        if not s.owner_name:
            return True
        return any(self.is_owner(n, s) for n in self.present_people())

    # --- authority gate ---
    def authority_active(self, s: Settings) -> bool:
        """True if owner-level commands may run right now."""
        if not s.obey_owner_only:
            return True
        if self.owner_present(s):
            return True
        return time.time() < self._approval_until

    def grant_window(self, seconds: float = APPROVAL_WINDOW):
        self._approval_until = time.time() + seconds

    # --- approval flow ("the robot asks its maker") ---
    async def request_approval(self, tool: str, args: dict, requester: str, reason: str) -> Pending:
        with self._lock:
            self._seq += 1
            pid = f"appr_{self._seq}_{int(time.time())}"
            p = Pending(id=pid, tool=tool, args=args, requester=requester, reason=reason)
            self._pending[pid] = p
        if self.emit:
            await self.emit({"type": "approval_request", "id": p.id, "tool": tool, "args": args,
                             "requester": requester, "reason": reason, "ts": p.ts})
        return p

    async def resolve(self, pid: str, approved: bool) -> bool:
        with self._lock:
            p = self._pending.pop(pid, None)
            if approved:
                self.grant_window()
        if self.emit:
            await self.emit({"type": "approval_resolved", "id": pid, "approved": approved,
                             "ts": time.time()})
        return p is not None

    def pending(self) -> list[dict]:
        with self._lock:
            return [{"id": p.id, "tool": p.tool, "args": p.args, "requester": p.requester,
                     "reason": p.reason, "ts": p.ts} for p in self._pending.values()]

    # --- prompt injection ---
    def summary_for_prompt(self, s: Settings) -> str:
        people = self.present_people()
        parts = []
        if s.owner_name:
            parts.append(f"Your owner/maker is {s.owner_name}.")
        if self._recognizer_active:
            parts.append(f"People you can see right now: {', '.join(people) if people else 'nobody recognized'}.")
        if s.obey_owner_only:
            if self.authority_active(s):
                parts.append("Owner authority is ACTIVE — you may carry out commands.")
            else:
                parts.append("Owner authority is NOT active — for any physical command from someone who is "
                             "not your owner, you must ask your owner for approval first.")
        return " ".join(parts)
