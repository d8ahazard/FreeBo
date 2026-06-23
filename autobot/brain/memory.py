"""Persistent memory for Autobot — what turns a stateless agent loop into something with continuity.

Three layers, all plain JSON/JSONL files under a data dir (default `data/memory/`), so memory survives
restarts and is easy to inspect/back up:

  - facts.json      — curated long-term facts (the AI's "long-term memory"): owner prefs, names, places.
  - daily/<date>.jsonl — append-only log of notable events (the raw daily notes).
  - sightings.jsonl — append-only log of what/who the robot has seen (fed by the recognition skill).

This mirrors the workspace's own SOUL/MEMORY pattern (daily notes -> curated long-term memory). Everything
fails soft: if the disk write fails, we log and keep running. Recall is simple keyword scoring for now; a
vector store can slot in behind the same interface later.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import embeddings

DEFAULT_DIR = os.environ.get("AUTOBOT_MEMORY_DIR", "data/memory")
# Append-only logs roll over to a single `.1` backup past this size so they never grow without bound.
_LOG_MAX_BYTES = int(os.environ.get("AUTOBOT_LOG_MAX_BYTES", str(1_000_000)))
# Daily note files older than this are pruned during the daily summarization pass.
_DAILY_KEEP_DAYS = int(os.environ.get("AUTOBOT_DAILY_KEEP_DAYS", "21"))


@dataclass
class Fact:
    text: str
    kind: str = "fact"          # fact | preference | person | place | event
    ts: float = field(default_factory=time.time)
    source: str = "ai"          # ai | owner | system

    def to_dict(self) -> dict:
        return {"text": self.text, "kind": self.kind, "ts": self.ts, "source": self.source}


class Memory:
    def __init__(self, base_dir: str = DEFAULT_DIR):
        self.dir = Path(base_dir)
        self.daily_dir = self.dir / "daily"
        self.facts_path = self.dir / "facts.json"
        self.sightings_path = self.dir / "sightings.jsonl"
        self._lock = threading.RLock()
        self._facts: list[Fact] = []
        self._vec_cache: dict[str, list[float]] = {}   # text -> embedding (semantic recall; lazily filled)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.daily_dir.mkdir(parents=True, exist_ok=True)
            self._load_facts()
        except Exception as e:  # noqa: BLE001 - fail soft; memory is best-effort
            print(f"[memory] init failed ({e}); running with in-RAM memory only", flush=True)

    # --- facts (long-term) ---
    def _load_facts(self):
        if self.facts_path.exists():
            data = json.loads(self.facts_path.read_text(encoding="utf-8") or "[]")
            self._facts = [Fact(**d) for d in data]

    def _save_facts(self):
        try:
            # Atomic: write a temp file then replace, so a crash mid-write can't corrupt facts.json.
            payload = json.dumps([f.to_dict() for f in self._facts], indent=2)
            tmp = self.facts_path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.facts_path)
        except Exception as e:  # noqa: BLE001
            print(f"[memory] fact save failed: {e}", flush=True)

    def remember(self, text: str, kind: str = "fact", source: str = "ai") -> Fact:
        """Add a long-term fact (deduped on identical text) and log it to today's notes."""
        text = (text or "").strip()
        with self._lock:
            f = Fact(text=text, kind=kind, source=source)
            if text and not any(x.text.lower() == text.lower() for x in self._facts):
                self._facts.append(f)
                self._save_facts()
            self.log_event(f"remembered ({kind}): {text}", source=source)
            return f

    def forget(self, query: str) -> int:
        """Drop facts whose text contains `query` (case-insensitive). Returns count removed."""
        q = (query or "").lower().strip()
        if not q:
            return 0
        with self._lock:
            before = len(self._facts)
            self._facts = [f for f in self._facts if q not in f.text.lower()]
            removed = before - len(self._facts)
            if removed:
                self._save_facts()
                self._vec_cache.clear()
            return removed

    def recall(self, query: str, limit: int = 5) -> list[Fact]:
        """Recall facts most relevant to `query`. Uses semantic (embedding) similarity when an embedding
        backend is configured (AUTOBOT_EMBED_MODEL), otherwise falls back to keyword scoring. This may make
        a network call to the embedding endpoint, so callers on the event loop should run it in a thread."""
        with self._lock:
            facts = list(self._facts)
        q = (query or "").strip()
        if not q:
            return sorted(facts, key=lambda f: f.ts, reverse=True)[:limit]
        sem = self._recall_semantic(q, facts, limit) if embeddings.embeddings_enabled() else None
        if sem is not None:
            return sem
        return self._recall_keyword(q, facts, limit)

    def _recall_keyword(self, query: str, facts: list[Fact], limit: int) -> list[Fact]:
        q_terms = [t for t in query.lower().split() if t]
        scored = []
        for f in facts:
            low = f.text.lower()
            score = sum(low.count(t) for t in q_terms)
            if score:
                scored.append((score, f))
        scored.sort(key=lambda s: (s[0], s[1].ts), reverse=True)
        return [f for _, f in scored[:limit]]

    def _recall_semantic(self, query: str, facts: list[Fact], limit: int) -> list[Fact] | None:
        """Embedding cosine-similarity recall. Returns None (so the caller falls back) on any failure."""
        if not facts:
            return []
        missing = [f.text for f in facts if f.text not in self._vec_cache]
        if missing:
            vecs = embeddings.embed(missing)
            if vecs is None:
                return None
            for text, v in zip(missing, vecs):
                self._vec_cache[text] = v
        qv = embeddings.embed([query])
        if not qv:
            return None
        qvec = qv[0]
        scored = [(embeddings.cosine(qvec, self._vec_cache.get(f.text, [])), f) for f in facts]
        scored = [(sc, f) for sc, f in scored if sc > 0.0]
        scored.sort(key=lambda s: s[0], reverse=True)
        return [f for _, f in scored[:limit]]

    def all_facts(self) -> list[Fact]:
        with self._lock:
            return list(self._facts)

    # --- daily log + sightings (append-only) ---
    def _append_jsonl(self, path: Path, obj: dict):
        try:
            # Roll over to a single `.1` backup once the log gets big, so it never grows unbounded.
            if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj) + "\n")
        except Exception as e:  # noqa: BLE001
            print(f"[memory] append failed ({path.name}): {e}", flush=True)

    def log_event(self, text: str, source: str = "ai"):
        day = date.today().isoformat()
        self._append_jsonl(self.daily_dir / f"{day}.jsonl",
                           {"ts": time.time(), "text": text, "source": source})

    def log_sighting(self, label: str, kind: str = "object", detail: str = ""):
        """Record that the robot saw something/someone (used by the recognition skill)."""
        self._append_jsonl(self.sightings_path,
                           {"ts": time.time(), "label": label, "kind": kind, "detail": detail})

    def recent_events(self, days: int = 2) -> list[str]:
        """Read the last `days` daily-note files' event texts (newest day last)."""
        out: list[str] = []
        try:
            files = sorted(self.daily_dir.glob("*.jsonl"))[-max(1, days):]
        except Exception:  # noqa: BLE001
            return out
        for p in files:
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        obj = json.loads(line)
                        out.append(str(obj.get("text", "")))
            except Exception:  # noqa: BLE001
                continue
        return out

    def replace_facts(self, facts: list[Fact]):
        """Wholesale replace the curated long-term facts (used by the daily summarizer)."""
        with self._lock:
            self._facts = list(facts)
            self._save_facts()
            self._vec_cache.clear()

    def prune_daily(self, keep_days: int = _DAILY_KEEP_DAYS) -> int:
        """Delete daily-note files older than the newest `keep_days`. Returns count removed. The summarizer
        has already distilled their durable content into facts.json, so old raw notes are safe to drop."""
        removed = 0
        try:
            files = sorted(self.daily_dir.glob("*.jsonl"))
            for p in files[:-keep_days] if len(files) > keep_days else []:
                p.unlink()
                removed += 1
        except Exception:  # noqa: BLE001
            pass
        return removed

    def clear(self) -> dict:
        """Wipe all memory (facts + daily notes + sightings) — a clean slate. Enrolled owner faces are NOT
        touched (that's identity/pairing, not memory)."""
        removed = 0
        with self._lock:
            self._facts = []
            self._save_facts()
            self._vec_cache.clear()
        try:
            for p in self.daily_dir.glob("*.jsonl"):
                p.unlink(); removed += 1
        except Exception:  # noqa: BLE001
            pass
        for p in (self.sightings_path, self.dir / "places.jsonl"):
            try:
                if p.exists():
                    p.unlink(); removed += 1
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "cleared_files": removed}

    def recent_sightings(self, limit: int = 10) -> list[dict]:
        if not self.sightings_path.exists():
            return []
        try:
            lines = self.sightings_path.read_text(encoding="utf-8").splitlines()[-limit:]
            return [json.loads(l) for l in lines if l.strip()]
        except Exception:  # noqa: BLE001
            return []

    # --- prompt injection ---
    def summary_for_prompt(self, max_facts: int = 12) -> str:
        """A compact memory block for the system prompt. Empty string if nothing remembered yet."""
        with self._lock:
            facts = sorted(self._facts, key=lambda f: f.ts, reverse=True)[:max_facts]
        if not facts:
            return ""
        lines = [f"- ({f.kind}) {f.text}" for f in facts]
        sights = self.recent_sightings(5)
        out = "WHAT YOU REMEMBER:\n" + "\n".join(lines)
        if sights:
            seen = ", ".join(f"{s.get('label')}" for s in sights)
            out += f"\nRecently seen: {seen}"
        return out
