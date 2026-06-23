"""Tasks / scheduling — give the robot things to do later or on a schedule.

A small persistent store of Tasks (JSON at AUTOBOT_TASKS_PATH, default data/tasks.json) plus the scheduling
math. The brain runs a scheduler loop that asks `due()` what should fire now and injects each task's text into
the agent as a high-priority instruction (so the robot reasons + acts on it through the normal safety floor).

A task fires on whichever schedule is set:
  - `in_seconds`  -> one-shot, N seconds from creation (e.g. "remind me in 20 minutes").
  - `daily_time`  -> repeats every day at "HH:MM" local (e.g. "patrol the house at 09:00").
  - `every_seconds` -> repeats on a fixed interval.

Everything is fail-soft and stdlib-only. See docs/AI_BRAIN.md.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

DEFAULT_PATH = os.environ.get("AUTOBOT_TASKS_PATH", "data/tasks.json")


@dataclass
class Task:
    id: str
    text: str                          # the instruction the robot runs when this fires
    daily_time: str | None = None      # "HH:MM" local, repeats daily
    every_seconds: float | None = None  # repeat interval
    enabled: bool = True
    created: float = field(default_factory=time.time)
    next_run: float | None = None
    last_run: float | None = None
    runs: int = 0

    def schedule_label(self) -> str:
        if self.daily_time:
            return f"daily at {self.daily_time}"
        if self.every_seconds:
            return f"every {int(self.every_seconds)}s"
        return "once"


def _next_daily(hhmm: str, now: float) -> float | None:
    try:
        h, m = (int(x) for x in hhmm.split(":", 1))
    except Exception:  # noqa: BLE001
        return None
    dt = datetime.fromtimestamp(now)
    target = dt.replace(hour=h, minute=m, second=0, microsecond=0)
    if target.timestamp() <= now:
        target = target + timedelta(days=1)
    return target.timestamp()


class TaskStore:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._load()

    # --- persistence ---
    def _load(self) -> None:
        try:
            if os.path.isfile(self.path):
                data = json.loads(open(self.path, encoding="utf-8").read() or "[]")
                for d in data:
                    t = Task(**d)
                    self._tasks[t.id] = t
        except Exception as e:  # noqa: BLE001
            print(f"[tasks] load failed: {e}", flush=True)

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            payload = json.dumps([asdict(t) for t in self._tasks.values()], indent=2)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            print(f"[tasks] save failed: {e}", flush=True)

    # --- mutations ---
    def add(self, text: str, *, in_seconds: float | None = None, daily_time: str | None = None,
            every_seconds: float | None = None) -> Task:
        now = time.time()
        tid = f"task_{int(now)}_{len(self._tasks) + 1}"
        t = Task(id=tid, text=text.strip(), daily_time=daily_time, every_seconds=every_seconds)
        if daily_time:
            t.next_run = _next_daily(daily_time, now)
        elif every_seconds:
            t.next_run = now + float(every_seconds)
        else:
            t.next_run = now + float(in_seconds if in_seconds is not None else 0)
        with self._lock:
            self._tasks[tid] = t
            self._save()
        return t

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            ok = self._tasks.pop(task_id, None) is not None
            if ok:
                self._save()
            return ok

    def list(self) -> list[Task]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: (t.next_run or 0))

    # --- scheduling ---
    def due(self, now: float | None = None) -> list[Task]:
        """Return tasks that should fire now, advancing/retiring their schedules. Persists changes."""
        now = now or time.time()
        fired: list[Task] = []
        changed = False
        with self._lock:
            for t in self._tasks.values():
                if not t.enabled or t.next_run is None or now < t.next_run:
                    continue
                t.last_run = now
                t.runs += 1
                fired.append(t)
                changed = True
                if t.daily_time:
                    t.next_run = _next_daily(t.daily_time, now)
                elif t.every_seconds:
                    t.next_run = now + float(t.every_seconds)
                else:
                    t.enabled = False          # one-shot: done
                    t.next_run = None
            if changed:
                self._save()
        return fired

    def summary_for_prompt(self) -> str:
        items = [t for t in self.list() if t.enabled]
        if not items:
            return ""
        lines = [f"- [{t.id}] {t.text} ({t.schedule_label()})" for t in items[:10]]
        return "YOUR SCHEDULED TASKS:\n" + "\n".join(lines)
