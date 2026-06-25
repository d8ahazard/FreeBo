"""Phase 1 observability (agent_next_3 Gate C): one canonical, append-only structured event model + a bounded
in-memory journal with durable JSONL persistence.

This is the single source of truth for safety/faculty/reasoning/transport/motion events. It is deliberately
dependency-light (stdlib only) and FAIL-SAFE: a persistence failure is surfaced (counter + optional callback)
but never raises into a safety path and never blocks a priority STOP. Secrets, tokens, prompts, audio, and image
bytes are redacted at the journal boundary.
"""
from __future__ import annotations

import itertools
import json
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ---- categories (agent_next_3 §C1) ----
CAT_SAFETY_TRANSITION = "safety.transition"
CAT_FACULTY = "safety.faculty_decision"
CAT_EFFECT = "control.effect_admission"
CAT_TRANSPORT = "control.transport"
CAT_REASON = "reason.lifecycle"
CAT_REASON_TOOL = "reason.tool"
CAT_SPEECH = "speech.lifecycle"
CAT_VISION = "vision.lifecycle"
CAT_MOTION = "motion.lifecycle"
CAT_SYSTEM = "system.lifecycle"
CATEGORIES = (CAT_SAFETY_TRANSITION, CAT_FACULTY, CAT_EFFECT, CAT_TRANSPORT, CAT_REASON, CAT_REASON_TOOL,
              CAT_SPEECH, CAT_VISION, CAT_MOTION, CAT_SYSTEM)

# ---- redaction (never persist these) ----
# Any dict key containing one of these tokens (case-insensitive) has its value masked. Strings longer than the
# cap are truncated. This is enforced at the journal boundary on the `detail` object.
_REDACT_TOKENS = ("api_key", "apikey", "token", "authkey", "license", "uid", "identity", "password", "passwd",
                  "secret", "credential", "prompt", "audio", "image", "jpeg", "jpg", "png", "wav", "g711",
                  "pcm", "b64", "base64", "frame_bytes")
_MAX_STR = 2000
_MAX_DETAIL_KEYS = 50


def _redact(obj: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "<max-depth>"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= _MAX_DETAIL_KEYS:
                out["__truncated_keys__"] = True
                break
            ks = str(k)
            if any(tok in ks.lower() for tok in _REDACT_TOKENS):
                out[ks] = "<redacted>"
            else:
                out[ks] = _redact(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact(v, depth + 1) for v in list(obj)[:100]]
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return f"<bytes:{len(obj)}>"
    if isinstance(obj, str):
        return obj if len(obj) <= _MAX_STR else (obj[:_MAX_STR] + "…<truncated>")
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _redact(str(obj), depth + 1)


@dataclass
class Event:
    id: str
    seq: int
    ts_monotonic: float
    ts_utc: str
    category: str
    type: str
    source: str
    requested: Optional[str] = None
    effective: Optional[str] = None
    reason: Optional[str] = None
    outcome: Optional[str] = None
    process_instance_id: Optional[str] = None
    sidecar_instance_id: Optional[str] = None
    epoch: Optional[int] = None
    generation: Optional[int] = None
    correlation_id: Optional[str] = None
    command_id: Optional[Any] = None
    ticket_id: Optional[int] = None
    latency_ms: Optional[float] = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def validate_event_dict(d: dict) -> bool:
    """A persisted/queried event must carry the required envelope fields with the right shapes."""
    if not isinstance(d, dict):
        return False
    for k in ("id", "seq", "ts_monotonic", "ts_utc", "category", "type", "source"):
        if k not in d or d[k] is None:
            return False
    return d["category"] in CATEGORIES and isinstance(d["seq"], int)


class EventJournal:
    """Bounded in-memory ring + durable JSONL. Thread-safe; minimally-blocking writes under a lock; rotation by
    size with a retention limit; redaction at the boundary; deterministic shutdown flush; truncated-final-line
    tolerant recovery. A write failure increments `dropped_persist` and (optionally) calls `on_error` — it never
    raises and never blocks the caller's safety path."""

    def __init__(self, path: Optional[str | Path] = None, *, max_mem: int = 4000,
                 max_bytes: int = 5_000_000, max_files: int = 5,
                 on_error: Optional[Callable[[Exception], None]] = None) -> None:
        self.path = Path(path) if path else None
        self.max_bytes = int(max_bytes)
        self.max_files = int(max_files)
        self.on_error = on_error
        self._mem: deque[dict] = deque(maxlen=int(max_mem))
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._fh = None
        self.dropped_persist = 0
        self.persist_ok = True
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self.path.open("a", encoding="utf-8")
            except Exception as e:  # noqa: BLE001 - persistence is best-effort; never fatal
                self.persist_ok = False
                self._surface(e)

    def _surface(self, e: Exception) -> None:
        self.dropped_persist += 1
        self.persist_ok = False
        if self.on_error is not None:
            try:
                self.on_error(e)
            except Exception:  # noqa: BLE001
                pass

    def emit(self, category: str, type: str, source: str, **fields: Any) -> Event:
        """Build, redact, ring-buffer, and persist ONE event. Returns the Event (also usable by the WS surface).
        Unknown categories are coerced to system.lifecycle with a note so a bad call never loses the record."""
        detail = _redact(fields.pop("detail", {}) or {})
        if category not in CATEGORIES:
            detail = {**detail, "__bad_category__": category}
            category = CAT_SYSTEM
        ev = Event(
            id=uuid.uuid4().hex, seq=next(self._seq), ts_monotonic=time.monotonic(),
            ts_utc=datetime.now(timezone.utc).isoformat(), category=category, type=str(type), source=str(source),
            requested=fields.get("requested"), effective=fields.get("effective"), reason=fields.get("reason"),
            outcome=fields.get("outcome"), process_instance_id=fields.get("process_instance_id"),
            sidecar_instance_id=fields.get("sidecar_instance_id"), epoch=fields.get("epoch"),
            generation=fields.get("generation"), correlation_id=fields.get("correlation_id"),
            command_id=fields.get("command_id"), ticket_id=fields.get("ticket_id"),
            latency_ms=fields.get("latency_ms"), detail=detail,
        )
        d = ev.to_dict()
        with self._lock:
            self._mem.append(d)
            self._persist_locked(d)
        return ev

    def _persist_locked(self, d: dict) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            self._fh.flush()
            if self.max_bytes > 0 and self._fh.tell() >= self.max_bytes:
                self._rotate_locked()
        except Exception as e:  # noqa: BLE001 - never let persistence break a safety path
            self._surface(e)

    def _rotate_locked(self) -> None:
        if self.path is None:
            return
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            # shift path.(n) -> path.(n+1), dropping the oldest beyond retention
            for i in range(self.max_files - 1, 0, -1):
                src = self.path.with_suffix(self.path.suffix + f".{i}")
                dst = self.path.with_suffix(self.path.suffix + f".{i + 1}")
                if src.exists():
                    if i + 1 > self.max_files:
                        src.unlink(missing_ok=True)
                    else:
                        os.replace(src, dst)
            if self.path.exists():
                os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
            self._fh = self.path.open("a", encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            self._surface(e)

    # ---- queries (bounded; never load an unbounded file into memory for one request) ----
    def query(self, *, category: Optional[str] = None, type: Optional[str] = None, source: Optional[str] = None,
              outcome: Optional[str] = None, correlation_id: Optional[str] = None, epoch: Optional[int] = None,
              generation: Optional[int] = None, since_seq: int = 0, limit: int = 200) -> dict:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = list(self._mem)
        out = []
        for d in rows:
            if d["seq"] <= since_seq:
                continue
            if category is not None and d["category"] != category:
                continue
            if type is not None and d["type"] != type:
                continue
            if source is not None and d["source"] != source:
                continue
            if outcome is not None and d.get("outcome") != outcome:
                continue
            if correlation_id is not None and d.get("correlation_id") != correlation_id:
                continue
            if epoch is not None and d.get("epoch") != epoch:
                continue
            if generation is not None and d.get("generation") != generation:
                continue
            out.append(d)
        next_cursor = out[limit - 1]["seq"] if len(out) > limit else (out[-1]["seq"] if out else since_seq)
        return {"events": out[:limit], "next_cursor": next_cursor, "returned": min(len(out), limit),
                "more": len(out) > limit}

    def correlation_trace(self, correlation_id: str, limit: int = 500) -> list[dict]:
        return self.query(correlation_id=correlation_id, limit=limit)["events"]

    def recent(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = list(self._mem)
        return rows[-max(1, min(int(limit), 1000)):]

    def summary(self, *, since_seq: int = 0) -> dict:
        with self._lock:
            rows = [d for d in self._mem if d["seq"] > since_seq]
        by_cat: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        lat: list[float] = []
        for d in rows:
            by_cat[d["category"]] = by_cat.get(d["category"], 0) + 1
            oc = d.get("outcome")
            if oc:
                by_outcome[oc] = by_outcome.get(oc, 0) + 1
            if isinstance(d.get("latency_ms"), (int, float)):
                lat.append(float(d["latency_ms"]))
        return {"total": len(rows), "by_category": by_cat, "by_outcome": by_outcome,
                "latency_ms": {"p50": _pct(lat, 50), "p95": _pct(lat, 95), "max": (max(lat) if lat else None)},
                "dropped_persist": self.dropped_persist, "persist_ok": self.persist_ok}

    def flush_and_close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                except Exception as e:  # noqa: BLE001
                    self._surface(e)
                self._fh = None

    @staticmethod
    def recover(path: str | Path) -> list[dict]:
        """Read a JSONL journal tolerating a TRUNCATED final line (a crash mid-write). Malformed lines are
        skipped, not fatal."""
        p = Path(path)
        if not p.exists():
            return []
        events: list[dict] = []
        with p.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001 - a truncated/partial final line is expected after a crash
                continue
            if validate_event_dict(d):
                events.append(d)
        return events


def _pct(xs: list[float], p: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    import math
    rank = max(1, math.ceil((p / 100.0) * len(s)))
    return s[min(rank, len(s)) - 1]


# ---- process-wide singleton (wired by the web server at startup) ----
_JOURNAL: Optional[EventJournal] = None


def configure(path: Optional[str | Path] = None, **kw: Any) -> EventJournal:
    global _JOURNAL
    _JOURNAL = EventJournal(path, **kw)
    return _JOURNAL


def journal() -> Optional[EventJournal]:
    return _JOURNAL


def emit(category: str, type: str, source: str, **fields: Any) -> Optional[Event]:
    """Module-level emit used by instrumentation. No-op (safe) if the journal isn't configured yet."""
    j = _JOURNAL
    if j is None:
        return None
    try:
        return j.emit(category, type, source, **fields)
    except Exception:  # noqa: BLE001 - observability must never break a runtime/safety path
        return None
