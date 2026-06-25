"""Phase 1 observability v2 (agent_next_4): one canonical append-only structured event model + a restart-aware,
non-blocking, redacting event journal with durable JSONL persistence and a causal incident model.

Design (fail-safe, stdlib-only):
  * `emit()` is synchronous only for: build + FULL-envelope redaction + insertion into a bounded in-memory ring.
    Durable writes go to a bounded queue drained by ONE background writer thread (batched + periodically
    flushed). When the queue is full we NEVER block a safety path — we keep the event in the ring and count the
    loss.
  * Restart-aware: at configure() we stream-recover the newest bounded history from the active + rotated JSONL
    (tolerating truncated/malformed lines), restore the ring, and CONTINUE the durable monotonic sequence so
    cursors never collide across restarts. An opaque cursor additionally encodes the process_session_id.
  * Causal model: every event carries process_session_id + (optional) incident_id / parent_event_id / phase, so
    a whole STOP→RESET lifecycle is one ordered incident query.
  * Redaction at the boundary covers EVERY untrusted free-text/structured field; secrets/tokens/prompts/audio/
    image bytes/memory contents are never persisted.
A journal failure is surfaced (counters + a health event) but never raises into a safety path.
"""
from __future__ import annotations

import base64
import contextlib
import json
import math
import os
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

# ---- categories (agent_next_3 §C1 / agent_next_4 §4) ----
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
_REDACT_TOKENS = ("api_key", "apikey", "token", "authkey", "license", "uid", "identity", "password", "passwd",
                  "secret", "credential", "prompt", "transcript", "caption_text", "spoken", "audio", "image",
                  "jpeg", "jpg", "png", "wav", "g711", "pcm", "b64", "base64", "frame_bytes", "memory")
_MAX_STR = 600           # envelope free-text fields are short labels; cap hard
_MAX_DETAIL_STR = 2000
_MAX_DETAIL_KEYS = 50


def _norm_text(v: Any, cap: int = _MAX_STR) -> Optional[str]:
    """Normalize an untrusted envelope free-text field: coerce to str, strip control chars, bound length. Keeps
    useful identifiers; it is the caller's responsibility not to pass secrets here (those belong nowhere)."""
    if v is None:
        return None
    s = str(v)
    s = "".join(ch for ch in s if ch == "\t" or ch >= " ")   # drop newlines/control chars
    return s if len(s) <= cap else (s[:cap] + "…")


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
        return obj if len(obj) <= _MAX_DETAIL_STR else (obj[:_MAX_DETAIL_STR] + "…<truncated>")
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _redact(str(obj), depth + 1)


@dataclass
class Event:
    id: str
    seq: int
    process_session_id: str
    ts_monotonic: float
    ts_utc: str
    category: str
    type: str
    source: str
    requested: Optional[str] = None
    effective: Optional[str] = None
    reason: Optional[str] = None
    outcome: Optional[str] = None
    incident_id: Optional[str] = None
    parent_event_id: Optional[str] = None
    phase: Optional[str] = None
    correlation_id: Optional[str] = None
    command_id: Optional[Any] = None
    ticket_id: Optional[int] = None
    process_instance_id: Optional[str] = None
    sidecar_instance_id: Optional[str] = None
    epoch: Optional[int] = None
    generation: Optional[int] = None
    latency_ms: Optional[float] = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def validate_event_dict(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    for k in ("id", "seq", "ts_monotonic", "ts_utc", "category", "type", "source", "process_session_id"):
        if k not in d or d[k] is None:
            return False
    return d["category"] in CATEGORIES and isinstance(d["seq"], int)


def encode_cursor(session_id: str, seq: int) -> str:
    """Opaque cursor encoding {process_session_id, seq} so it can never silently collide after a restart."""
    return base64.urlsafe_b64encode(f"{session_id}|{int(seq)}".encode()).decode()


def parse_cursor(cursor: Optional[str]) -> Optional[tuple[str, int]]:
    """Decode an opaque cursor to (process_session_id, seq), or None if malformed. The session component is
    preserved (not silently discarded) so the caller can validate the cursor's domain (agent_next_5 §1.2)."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        sid, seq = raw.rsplit("|", 1)
        return sid, int(seq)
    except Exception:  # noqa: BLE001
        return None


def decode_cursor(cursor: Optional[str]) -> Optional[int]:
    p = parse_cursor(cursor)
    return p[1] if p is not None else None


def _pct(xs: list[float], p: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    rank = max(1, math.ceil((p / 100.0) * len(s)))
    return s[min(rank, len(s)) - 1]


def _read_tail_lines(path: Path, max_lines: int, block: int = 65536) -> list[str]:
    """Read up to the last `max_lines` non-empty lines of a file using bounded reverse chunk reads (never loads
    the whole file when it is large). Returns them in file order (oldest→newest of the tail)."""
    if max_lines <= 0:
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            buf = b""
            reached_start = pos == 0
            while pos > 0 and buf.count(b"\n") <= max_lines:
                read = min(block, pos)
                pos -= read
                f.seek(pos)
                buf = f.read(read) + buf
                reached_start = pos == 0
        parts = buf.split(b"\n")
        if not reached_start and parts:
            parts = parts[1:]                       # first piece may be a partial line; drop it
        decoded = [p.decode("utf-8", "replace") for p in parts]
        return [ln for ln in decoded if ln.strip()][-max_lines:]
    except Exception:  # noqa: BLE001
        return []


def _tail_events(path: Path, max_events: int) -> list[dict]:
    """The newest <= max_events VALID events from a JSONL file (tolerates malformed/truncated lines)."""
    out: list[dict] = []
    for line in _read_tail_lines(path, max_events * 2):
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if validate_event_dict(d):
            out.append(d)
    return out[-max_events:]


def _stream_jsonl(path: Path, max_events: int) -> Iterator[dict]:
    """Stream a JSONL file line-by-line (NOT readlines), tolerating malformed/truncated lines. Bounded by
    max_events."""
    n = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if n >= max_events:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001 - truncated final line / corruption
                    continue
                if validate_event_dict(d):
                    n += 1
                    yield d
    except Exception:  # noqa: BLE001
        return


class EventJournal:
    def __init__(self, path: Optional[str | Path] = None, *, max_mem: int = 4000,
                 max_bytes: int = 5_000_000, max_files: int = 5, queue_max: int = 4000,
                 recover_max_events: int = 4000, flush_interval: float = 0.5,
                 on_error: Optional[Callable[[Exception], None]] = None) -> None:
        self.path = Path(path) if path else None
        self.max_bytes = int(max_bytes)
        self.max_files = int(max_files)
        self.recover_max_events = int(recover_max_events)
        self.flush_interval = float(flush_interval)
        self.on_error = on_error
        self.process_session_id = uuid.uuid4().hex
        self._mem: deque[dict] = deque(maxlen=int(max_mem))
        self._lock = threading.Lock()
        self._fh = None
        # counters
        self.enqueued = 0
        self.persisted = 0
        self.queue_dropped = 0
        self.persist_failed = 0
        self.undrained = 0
        self.recovered = 0
        self.persist_ok = True
        self._queue_full_logged = False
        self._oldest_ts: Optional[str] = None
        self._newest_ts: Optional[str] = None
        # durable monotonic seq: continue past recovered history
        self._seq_lock = threading.Lock()
        self._next_seq = 1
        # background writer
        self._wq: queue.Queue = queue.Queue(maxsize=int(queue_max))
        self._closing = threading.Event()
        self._close_deadline = 3.0
        self._writer: Optional[threading.Thread] = None
        if self.path is not None:
            with self._lock:
                self._recover_locked()
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._fh = self.path.open("a", encoding="utf-8")
                except Exception as e:  # noqa: BLE001
                    self.persist_ok = False
                    self._surface(e)
            self._writer = threading.Thread(target=self._writer_loop, name="obs-writer", daemon=True)
            self._writer.start()
            self.emit(CAT_SYSTEM, "journal_recovered", "journal", outcome="ok",
                      detail={"recovered": self.recovered, "next_seq": self._next_seq,
                              "oldest_ts": self._oldest_ts, "newest_ts": self._newest_ts})

    def _surface(self, e: Exception) -> None:
        self.persist_failed += 1
        self.persist_ok = False
        if self.on_error is not None:
            try:
                self.on_error(e)
            except Exception:  # noqa: BLE001
                pass

    def _retained_files(self) -> list[Path]:
        if self.path is None:
            return []
        files = [self.path] if self.path.exists() else []
        for i in range(1, self.max_files + 1):
            p = self.path.with_suffix(self.path.suffix + f".{i}")
            if p.exists():
                files.append(p)
        return files

    def _recover_locked(self) -> None:
        """Bounded NEWEST-tail recovery (agent_next_5 §1.3). Process retained files in true chronological order
        (active is newest, then .1, .2, …) and recover the newest `recover_max_events` via bounded reverse
        reading — NOT the first records encountered. A large active file can no longer restore its oldest rows
        or starve rotated history, and the durable sequence is continued from the true max seq seen."""
        budget = self.recover_max_events
        collected: list[dict] = []
        max_seq = 0
        # newest file first; pull each file's newest tail until the global budget is filled.
        files = [self.path] + [self.path.with_suffix(self.path.suffix + f".{i}") for i in range(1, self.max_files + 1)]
        for p in files:
            if p is None or not p.exists():
                continue
            remaining = budget - len(collected)
            if remaining <= 0:
                break
            collected.extend(_tail_events(p, remaining))
        for d in collected:
            if isinstance(d.get("seq"), int):
                max_seq = max(max_seq, d["seq"])
        # de-dup by id (rotation overlap is possible) + stable durable-seq order for the ring + ts bounds.
        seen: set = set()
        uniq = []
        for d in sorted(collected, key=lambda d: d.get("seq", 0)):
            if d.get("id") in seen:
                continue
            seen.add(d.get("id"))
            uniq.append(d)
        tail = uniq[-self._mem.maxlen:] if self._mem.maxlen else uniq
        for d in tail:
            self._mem.append(d)
        self.recovered = len(tail)
        self._next_seq = max_seq + 1
        if tail:
            self._oldest_ts = tail[0].get("ts_utc")
            self._newest_ts = tail[-1].get("ts_utc")

    def _alloc_seq(self) -> int:
        with self._seq_lock:
            s = self._next_seq
            self._next_seq += 1
            return s

    def emit(self, category: str, type: str, source: str, **fields: Any) -> Event:
        detail = _redact(fields.pop("detail", {}) or {})
        if category not in CATEGORIES:
            detail = {**detail, "__bad_category__": str(category)[:80]}
            category = CAT_SYSTEM
        ev = Event(
            id=uuid.uuid4().hex, seq=self._alloc_seq(), process_session_id=self.process_session_id,
            ts_monotonic=time.monotonic(), ts_utc=datetime.now(timezone.utc).isoformat(),
            category=category, type=(_norm_text(type) or "event"), source=(_norm_text(source) or "unknown"),
            requested=_norm_text(fields.get("requested")), effective=_norm_text(fields.get("effective")),
            reason=_norm_text(fields.get("reason")), outcome=_norm_text(fields.get("outcome")),
            incident_id=_norm_text(fields.get("incident_id")), parent_event_id=_norm_text(fields.get("parent_event_id")),
            phase=_norm_text(fields.get("phase")), correlation_id=_norm_text(fields.get("correlation_id")),
            command_id=_norm_text(fields.get("command_id"), cap=128) if fields.get("command_id") is not None else None,
            ticket_id=fields.get("ticket_id"), process_instance_id=_norm_text(fields.get("process_instance_id"), cap=64),
            sidecar_instance_id=_norm_text(fields.get("sidecar_instance_id"), cap=64),
            epoch=fields.get("epoch"), generation=fields.get("generation"), latency_ms=fields.get("latency_ms"),
            detail=detail,
        )
        d = ev.to_dict()
        with self._lock:
            self._mem.append(d)
            self._newest_ts = d["ts_utc"]
            if self._oldest_ts is None:
                self._oldest_ts = d["ts_utc"]
        # non-blocking enqueue for durable write; never block a safety path
        if self.path is not None:
            self.enqueued += 1
            try:
                self._wq.put_nowait(d)
                if self._queue_full_logged and self._wq.qsize() < (self._wq.maxsize // 2):
                    self._queue_full_logged = False   # capacity recovered; allow one health event later
            except queue.Full:
                self.queue_dropped += 1
                if not self._queue_full_logged:
                    self._queue_full_logged = True     # avoid recursively flooding the queue
        return ev

    def _writer_loop(self) -> None:
        # agent_next_5 §1.4: the writer ALWAYS observes closure via `_closing` (not only a queued sentinel — a
        # full queue can drop the sentinel). It wakes at least every flush_interval, so closure is observed even
        # when no sentinel can be enqueued. The None sentinel is only a best-effort wake.
        batch: list[dict] = []
        last_flush = time.monotonic()
        while not self._closing.is_set():
            try:
                item = self._wq.get(timeout=self.flush_interval)
                if item is not None:
                    batch.append(item)
                    while len(batch) < 256:
                        try:
                            nxt = self._wq.get_nowait()
                        except queue.Empty:
                            break
                        if nxt is not None:
                            batch.append(nxt)
            except queue.Empty:
                pass
            if batch and (time.monotonic() - last_flush >= self.flush_interval or len(batch) >= 64):
                self._flush_batch(batch)
                batch = []
                last_flush = time.monotonic()
        self._drain_and_close(batch)

    def _drain_and_close(self, batch: list[dict]) -> None:
        """Closure path, owned by the writer thread so the file is NEVER closed while it can still be written.
        Drain the queue until empty or the bounded deadline, flush, record the EXACT undrained count, then close
        the file here."""
        deadline = time.monotonic() + self._close_deadline
        while time.monotonic() < deadline:
            try:
                item = self._wq.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                batch.append(item)
            if len(batch) >= 256:
                self._flush_batch(batch)
                batch = []
        if batch:
            self._flush_batch(batch)
        self.undrained = self._wq.qsize()
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                except Exception as e:  # noqa: BLE001
                    self._surface(e)
                self._fh = None

    def _flush_batch(self, batch: list[dict]) -> None:
        if not batch or self._fh is None:
            return
        with self._lock:
            try:
                for d in batch:
                    self._fh.write(json.dumps(d, ensure_ascii=False) + "\n")
                self._fh.flush()
                self.persisted += len(batch)
                if self.max_bytes > 0 and self._fh.tell() >= self.max_bytes:
                    self._rotate_locked()
            except Exception as e:  # noqa: BLE001
                self._surface(e)
        batch.clear()

    def _rotate_locked(self) -> None:
        if self.path is None or self._fh is None:
            return
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass
        try:
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

    # ---- queries ----
    def _match(self, d: dict, f: dict) -> bool:
        for k in ("category", "type", "source", "outcome", "correlation_id", "incident_id",
                  "process_session_id", "command_id"):
            if f.get(k) is not None and d.get(k) != f[k]:
                return False
        if f.get("event_id") is not None and d.get("id") != f["event_id"]:
            return False
        for k in ("epoch", "generation", "ticket_id"):
            if f.get(k) is not None and d.get(k) != f[k]:
                return False
        if f.get("start") is not None and (d.get("ts_utc") or "") < f["start"]:
            return False
        if f.get("end") is not None and (d.get("ts_utc") or "") > f["end"]:
            return False
        return True

    def query(self, *, limit: int = 200, cursor: Optional[str] = None, order: str = "desc",
              persistent: bool = False, **filters: Any) -> dict:
        """Bounded, ORDER-AWARE, cursor-paginated query (agent_next_5 §1.2). Ascending walks forward
        (`seq > cursor_seq`); descending walks backward (`seq < cursor_seq`). The cursor encodes the session +
        durable seq; because seq is durable-monotonic across restarts it is a valid resume point even from a prior
        process session. A malformed cursor raises ValueError (the API maps it to HTTP 400). By default scans the
        in-memory ring; with `persistent=True` / a time-range / id filter it ALSO streams the retained JSONL
        window (bounded, de-duped by id)."""
        limit = max(1, min(int(limit), 1000))
        order = "asc" if order == "asc" else "desc"
        parsed = parse_cursor(cursor) if cursor else None
        if cursor and parsed is None:
            raise ValueError("malformed cursor")
        cursor_seq = parsed[1] if parsed is not None else None
        cursor_session = parsed[0] if parsed is not None else None
        with self._lock:
            rows = list(self._mem)
        need_persistent = persistent or any(filters.get(k) is not None for k in
                                            ("start", "end", "incident_id", "event_id", "command_id"))
        if need_persistent and self.path is not None:
            # §1.3: pull each retained file's NEWEST tail (bounded) so one large active file can't starve the
            # rotated history; bounded overall by recover_max_events and de-duped by id against the ring.
            seen = {d.get("id") for d in rows}
            budget = self.recover_max_events
            for p in self._retained_files():            # newest-first: active, .1, .2, …
                if budget <= 0:
                    break
                for d in _tail_events(p, budget):
                    if d.get("id") not in seen:
                        seen.add(d.get("id"))
                        rows.append(d)
                        budget -= 1

        def _after(seq: int) -> bool:
            if cursor_seq is None:
                return True
            return seq > cursor_seq if order == "asc" else seq < cursor_seq
        out = [d for d in rows if _after(d.get("seq", 0)) and self._match(d, filters)]
        out.sort(key=lambda d: d.get("seq", 0), reverse=(order != "asc"))
        page = out[:limit]
        more = len(out) > limit
        return {"events": page, "returned": len(page), "more": more,
                "next_cursor": (encode_cursor(self.process_session_id, page[-1]["seq"]) if page else cursor),
                "process_session_id": self.process_session_id,
                "cursor_session": cursor_session,
                "cursor_foreign_session": (cursor_session is not None
                                           and cursor_session != self.process_session_id)}

    def correlation_trace(self, correlation_id: str, limit: int = 1000) -> list[dict]:
        return self.query(correlation_id=correlation_id, limit=limit, order="asc")["events"]

    def incident_trace(self, incident_id: str, limit: int = 2000) -> list[dict]:
        return self.query(incident_id=incident_id, limit=limit, order="asc", persistent=True)["events"]

    def recent(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = list(self._mem)
        return rows[-max(1, min(int(limit), 1000)):]

    def incidents(self, *, limit: int = 50) -> list[dict]:
        """List recent incidents (by incident_id) with start/end/outcome/severity, from the ring."""
        with self._lock:
            rows = list(self._mem)
        groups: dict[str, dict] = {}
        for d in rows:
            iid = d.get("incident_id")
            if not iid:
                continue
            g = groups.setdefault(iid, {"incident_id": iid, "start_ts": d["ts_utc"], "end_ts": d["ts_utc"],
                                        "count": 0, "outcome": None, "severity": "info", "first_seq": d["seq"]})
            g["count"] += 1
            g["end_ts"] = d["ts_utc"]
            if d.get("outcome"):
                g["outcome"] = d["outcome"]
            oc = (d.get("outcome") or d.get("effective") or "").lower()
            if any(b in oc for b in ("degraded", "failed", "critical")):
                g["severity"] = "critical"
            elif g["severity"] != "critical" and any(b in oc for b in ("denied", "cancelled", "inhibited")):
                g["severity"] = "warn"
        return sorted(groups.values(), key=lambda g: g["first_seq"], reverse=True)[:limit]

    def summary(self, *, cursor: Optional[str] = None) -> dict:
        since_seq = decode_cursor(cursor) or 0
        with self._lock:
            rows = [d for d in self._mem if d["seq"] > since_seq]
        by_cat: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        lat: list[float] = []
        for d in rows:
            by_cat[d["category"]] = by_cat.get(d["category"], 0) + 1
            if d.get("outcome"):
                by_outcome[d["outcome"]] = by_outcome.get(d["outcome"], 0) + 1
            if isinstance(d.get("latency_ms"), (int, float)):
                lat.append(float(d["latency_ms"]))
        return {"total": len(rows), "by_category": by_cat, "by_outcome": by_outcome,
                "latency_ms": {"p50": _pct(lat, 50), "p95": _pct(lat, 95), "max": (max(lat) if lat else None)},
                "health": self.health()}

    def health(self) -> dict:
        active_size = 0
        with self._lock:
            if self._fh is not None:
                try:
                    active_size = self._fh.tell()
                except Exception:  # noqa: BLE001
                    active_size = 0
        return {
            "writer_alive": bool(self._writer and self._writer.is_alive()),
            "queue_depth": self._wq.qsize(), "queue_capacity": self._wq.maxsize,
            "enqueued": self.enqueued, "persisted": self.persisted, "queue_dropped": self.queue_dropped,
            "persist_failed": self.persist_failed, "undrained": self.undrained, "recovered": self.recovered,
            "persist_ok": self.persist_ok, "active_file_bytes": active_size,
            "retained_files": len(self._retained_files()),
            "oldest_ts": self._oldest_ts, "newest_ts": self._newest_ts,
            "process_session_id": self.process_session_id,
        }

    def flush_and_close(self, *, deadline_s: float = 3.0) -> None:
        """Deterministic, idempotent shutdown (agent_next_5 §1.4). Signals closure (the writer ALWAYS observes it,
        even if the queue is full and no sentinel can be enqueued), joins within a bounded deadline, and lets the
        writer drain + close the file itself so the file is never closed while the writer can still write. A
        no-path (memory-only) journal or a repeat call is a clean no-op."""
        self._close_deadline = deadline_s
        self._closing.set()
        with contextlib.suppress(queue.Full):
            self._wq.put_nowait(None)       # best-effort immediate wake; correctness does NOT depend on it
        if self._writer is not None and self._writer.is_alive():
            self._writer.join(timeout=deadline_s + max(0.5, self.flush_interval * 2))
        # Defensive close ONLY when the writer is gone (or never existed); never close under a live writer.
        with self._lock:
            if self._fh is not None and (self._writer is None or not self._writer.is_alive()):
                try:
                    self._fh.flush()
                    self._fh.close()
                except Exception as e:  # noqa: BLE001
                    self._surface(e)
                self._fh = None
                self.undrained = self._wq.qsize()

    @staticmethod
    def recover(path: str | Path, max_events: int = 100000) -> list[dict]:
        """Streaming recovery of a JSONL journal tolerating a truncated final line (bounded)."""
        return list(_stream_jsonl(Path(path), max_events))


# ---- process-wide singleton ----
_JOURNAL: Optional[EventJournal] = None


def configure(path: Optional[str | Path] = None, **kw: Any) -> EventJournal:
    """Configure (or replace) the process journal. agent_next_4 §8.1: cleanly drain/close any existing journal
    before replacement (also matters in tests)."""
    global _JOURNAL
    old = _JOURNAL
    if old is not None:
        try:
            old.flush_and_close(deadline_s=2.0)
        except Exception:  # noqa: BLE001
            pass
    _JOURNAL = EventJournal(path, **kw)
    return _JOURNAL


def journal() -> Optional[EventJournal]:
    return _JOURNAL


# Optional hook so a transport (e.g. the web server) can broadcast each emitted event live. Set by the server.
_ON_EMIT: Optional[Callable[[Event], None]] = None


def set_on_emit(cb: Optional[Callable[[Event], None]]) -> None:
    global _ON_EMIT
    _ON_EMIT = cb


def emit(category: str, type: str, source: str, **fields: Any) -> Optional[Event]:
    """Module-level emit used by instrumentation. Safe no-op if the journal isn't configured. Never raises."""
    j = _JOURNAL
    if j is None:
        return None
    try:
        ev = j.emit(category, type, source, **fields)
    except Exception:  # noqa: BLE001 - observability must never break a runtime/safety path
        return None
    cb = _ON_EMIT
    if cb is not None:
        try:
            cb(ev)
        except Exception:  # noqa: BLE001
            pass
    return ev
