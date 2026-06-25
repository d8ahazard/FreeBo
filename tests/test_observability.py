"""agent_next_3 Gate C7 — observability journal/schema/redaction/persistence tests (no robot, no network)."""
from __future__ import annotations

import json

import pytest

from autobot import observability as obs
from autobot.observability import CAT_MOTION, CAT_SAFETY_TRANSITION, EventJournal, validate_event_dict


def test_event_has_required_envelope_and_validates():
    j = EventJournal(path=None)
    ev = j.emit(CAT_SAFETY_TRANSITION, "master_stop", "api", outcome="dispatched", epoch=2, generation=2)
    d = ev.to_dict()
    assert validate_event_dict(d)
    for k in ("id", "seq", "ts_monotonic", "ts_utc", "category", "type", "source"):
        assert d.get(k) is not None
    assert d["category"] == CAT_SAFETY_TRANSITION and d["outcome"] == "dispatched"


def test_bad_category_is_coerced_not_lost():
    j = EventJournal(path=None)
    ev = j.emit("not.a.category", "x", "test")
    assert ev.category == obs.CAT_SYSTEM
    assert ev.detail.get("__bad_category__") == "not.a.category"


def test_redaction_drops_secrets_and_payloads():
    j = EventJournal(path=None)
    ev = j.emit(CAT_MOTION, "drive", "ai", detail={
        "api_key": "sk-secret", "auth_token": "T", "ly": 0.3, "jpeg": b"\xff\xd8\xff", "nested": {"password": "p"}})
    det = ev.detail
    assert det["api_key"] == "<redacted>"
    assert det["auth_token"] == "<redacted>"
    assert det["ly"] == 0.3                       # non-secret kept
    assert det["jpeg"] == "<redacted>"            # key name matches a payload token
    assert det["nested"]["password"] == "<redacted>"


def test_bounded_memory_ring():
    j = EventJournal(path=None, max_mem=5)
    for i in range(20):
        j.emit(CAT_MOTION, "drive", "ai", detail={"i": i})
    rows = j.recent(100)
    assert len(rows) == 5                          # ring bounded
    assert [r["detail"]["i"] for r in rows] == [15, 16, 17, 18, 19]


def test_persistence_failure_is_surfaced_not_raised(tmp_path):
    errs = []
    j = EventJournal(path=tmp_path / "j.jsonl", on_error=errs.append)
    # simulate a broken file handle
    class _Broken:
        def write(self, *_a): raise OSError("disk full")
        def flush(self): raise OSError("disk full")
        def tell(self): return 0
        def close(self): pass
    j._fh = _Broken()
    j.emit(CAT_MOTION, "drive", "ai")             # must NOT raise
    assert j.dropped_persist >= 1 and j.persist_ok is False
    assert errs                                    # surfaced via callback


def test_jsonl_persistence_and_truncated_recovery(tmp_path):
    p = tmp_path / "j.jsonl"
    j = EventJournal(path=p)
    j.emit(CAT_SAFETY_TRANSITION, "master_stop", "api", correlation_id="c1")
    j.emit(CAT_MOTION, "drive", "ai", correlation_id="c1")
    j.flush_and_close()
    # append a TRUNCATED final line (simulating a crash mid-write)
    with p.open("a", encoding="utf-8") as f:
        f.write('{"id": "partial", "seq": 99, "category": "moti')
    recovered = EventJournal.recover(p)
    assert len(recovered) == 2                     # the two complete events; the truncated line is skipped
    assert all(validate_event_dict(d) for d in recovered)


def test_rotation_and_retention(tmp_path):
    p = tmp_path / "j.jsonl"
    j = EventJournal(path=p, max_bytes=400, max_files=3)
    for i in range(200):
        j.emit(CAT_MOTION, "drive", "ai", detail={"i": i, "pad": "x" * 50})
    j.flush_and_close()
    # at most max_files rotated files exist (retention enforced)
    rotated = list(tmp_path.glob("j.jsonl.*"))
    assert 1 <= len(rotated) <= 3
    assert p.exists()


def test_query_filter_and_pagination():
    j = EventJournal(path=None, max_mem=1000)
    for i in range(10):
        j.emit(CAT_MOTION, "drive", "ai", outcome=("moved" if i % 2 else "blocked"), correlation_id="trace")
    j.emit(CAT_SAFETY_TRANSITION, "master_stop", "api", correlation_id="trace")
    r1 = j.query(category=CAT_MOTION, outcome="moved")
    assert all(e["outcome"] == "moved" and e["category"] == CAT_MOTION for e in r1["events"])
    assert len(r1["events"]) == 5
    # pagination by cursor
    page = j.query(limit=4)
    assert page["returned"] == 4 and page["more"] is True
    page2 = j.query(since_seq=page["next_cursor"], limit=4)
    assert page2["events"][0]["seq"] > page["events"][-1]["seq"]
    # correlation trace gathers all in that trace
    trace = j.correlation_trace("trace")
    assert len(trace) == 11


def test_summary_counts_and_latency_percentiles():
    j = EventJournal(path=None)
    for ms in (10, 20, 30, 40, 50):
        j.emit(CAT_MOTION, "drive", "ai", outcome="moved", latency_ms=ms)
    s = j.summary()
    assert s["total"] == 5
    assert s["by_category"][CAT_MOTION] == 5
    assert s["by_outcome"]["moved"] == 5
    assert s["latency_ms"]["p95"] == 50


def test_module_emit_is_safe_before_configure(monkeypatch):
    monkeypatch.setattr(obs, "_JOURNAL", None)
    assert obs.emit(CAT_MOTION, "drive", "ai") is None    # no-op, no raise
