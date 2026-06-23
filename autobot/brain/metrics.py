"""Lightweight, dependency-free latency metrics for the brain loop.

The point (see docs/MATURITY.md §2): turn "feels responsive" into numbers. We record per-phase durations
(perceive, provider/LLM call, tool execution, full reason cycle, VLM eyes, reflex stop) into small rolling
windows and expose p50/p95/p99 so every release is comparable on identical traces.

Design constraints: stdlib only (this runs on a Pi), cheap (a perf_counter pair + a deque append under a
lock), and FAIL-SOFT — instrumentation must never throw into the control loop. Optional raw-sample export to
JSONL via AUTOBOT_METRICS_LOG (or an explicit log_path) for offline analysis.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from contextlib import contextmanager

DEFAULT_WINDOW = 256


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 0..100) over an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = math.ceil((pct / 100.0) * len(sorted_vals))
    idx = min(max(rank, 1), len(sorted_vals)) - 1
    return sorted_vals[idx]


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0, "max": 0.0, "last": 0.0}
    s = sorted(vals)
    return {
        "count": len(vals),
        "p50": round(_percentile(s, 50), 2),
        "p95": round(_percentile(s, 95), 2),
        "p99": round(_percentile(s, 99), 2),
        "mean": round(sum(vals) / len(vals), 2),
        "max": round(s[-1], 2),
        "last": round(vals[-1], 2),
    }


class Metrics:
    """Per-phase rolling latency windows (durations in milliseconds). Thread-safe; STT/audio callbacks may
    record from other threads, so a small lock guards the deques."""

    def __init__(self, window: int = DEFAULT_WINDOW, log_path: str | None = None):
        self._window = max(8, int(window))
        self._data: dict[str, deque] = {}
        self._lock = threading.Lock()
        # Raw-sample export (one JSON object per line). Explicit arg wins; else env. None disables.
        self._log_path = log_path if log_path is not None else (os.environ.get("AUTOBOT_METRICS_LOG") or None)

    def record(self, phase: str, ms: float) -> None:
        """Record one duration sample (ms) for a phase. Fail-soft: never raises into the caller."""
        try:
            with self._lock:
                dq = self._data.get(phase)
                if dq is None:
                    dq = self._data[phase] = deque(maxlen=self._window)
                dq.append(float(ms))
            if self._log_path:
                self._append_log(phase, ms)
        except Exception:  # noqa: BLE001 - metrics must never break the loop
            pass

    @contextmanager
    def timer(self, phase: str):
        """Time a block (including any `await` inside it) and record the elapsed ms on exit.

        Usage: `with metrics.timer("provider"): result = await provider.chat(...)`. The await happens inside
        the with-body, so the duration is recorded when the block exits — works for async code without an
        async context manager.
        """
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.record(phase, (time.perf_counter() - t0) * 1000.0)

    def _append_log(self, phase: str, ms: float) -> None:
        try:
            line = json.dumps({"ts": round(time.time(), 3), "phase": phase, "ms": round(float(ms), 3)})
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> dict:
        """Full per-phase stats (count/p50/p95/p99/mean/max/last in ms)."""
        with self._lock:
            items = {k: list(v) for k, v in self._data.items()}
        return {k: _stats(v) for k, v in items.items()}

    def summary(self) -> dict:
        """Compact view for the status payload: count + p50 + p95 (ms) per phase."""
        return {k: {"count": st["count"], "p50": st["p50"], "p95": st["p95"]}
                for k, st in self.snapshot().items()}

    def reset(self) -> None:
        with self._lock:
            self._data.clear()
