"""agent_next_4 — observability journal v2: schema, full-envelope redaction, bounded writer + drop accounting,
restart recovery across rotated files, cursor uniqueness across sessions, drain, health, time-range queries."""
from __future__ import annotations

import json
import time

import pytest

from autobot import observability as obs
from autobot.observability import (CAT_MOTION, CAT_SAFETY_TRANSITION, EventJournal, decode_cursor,
                                   encode_cursor, validate_event_dict)


def _drain(j, timeout=3.0):
    """Wait until the background writer has persisted everything enqueued (bounded)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if j._wq.qsize() == 0 and j.persisted >= (j.enqueued - j.queue_dropped):
            return
        time.sleep(0.02)


def test_event_envelope_has_session_and_validates():
    j = EventJournal(path=None)
    try:
        ev = j.emit(CAT_SAFETY_TRANSITION, "master_stop", "api", outcome="dispatched", epoch=2, generation=2,
                    incident_id="inc1", phase="dispatch")
        d = ev.to_dict()
        assert validate_event_dict(d)
        assert d["process_session_id"] == j.process_session_id
        assert d["incident_id"] == "inc1" and d["phase"] == "dispatch"
    finally:
        j.flush_and_close()


def test_full_envelope_redaction_and_normalization():
    j = EventJournal(path=None)
    try:
        ev = j.emit(CAT_MOTION, "drive\nINJECT", "ai",
                    reason="x" * 5000, requested="ok",
                    detail={"api_key": "sk-secret", "ly": 0.3, "jpeg": b"\xff", "nested": {"token": "T"}})
        assert "\n" not in ev.type                      # control chars stripped from envelope
        assert len(ev.reason) <= 601                     # bounded
        assert ev.detail["api_key"] == "<redacted>"
        assert ev.detail["jpeg"] == "<redacted>"
        assert ev.detail["nested"]["token"] == "<redacted>"
        assert ev.detail["ly"] == 0.3
    finally:
        j.flush_and_close()


def test_bounded_memory_ring():
    j = EventJournal(path=None, max_mem=5)
    try:
        for i in range(20):
            j.emit(CAT_MOTION, "drive", "ai", detail={"i": i})
        rows = j.recent(100)
        assert len(rows) == 5 and [r["detail"]["i"] for r in rows] == [15, 16, 17, 18, 19]
    finally:
        j.flush_and_close()


def test_background_writer_persists(tmp_path):
    j = EventJournal(path=tmp_path / "j.jsonl", flush_interval=0.05)
    try:
        for i in range(30):
            j.emit(CAT_MOTION, "drive", "ai", detail={"i": i})
        _drain(j)
        assert j.persisted >= 30 and j.health()["writer_alive"] is True
    finally:
        j.flush_and_close()
    # the events are durable
    recovered = EventJournal.recover(tmp_path / "j.jsonl")
    assert len([d for d in recovered if d["type"] == "drive"]) >= 30


def test_writer_queue_drop_accounting(tmp_path):
    # Deterministic: stop the writer, then overflow the tiny queue -> put_nowait drops are counted; ring keeps them.
    j = EventJournal(path=tmp_path / "j.jsonl", queue_max=4)
    j.flush_and_close()                                  # writer dead; queue no longer drained
    for i in range(50):
        j.emit(CAT_MOTION, "drive", "ai", detail={"i": i})
    h = j.health()
    assert h["queue_dropped"] > 0                        # overflow counted, never raised
    assert len(j.recent(100)) > 0                        # events still in the ring (not lost to the operator view)


def test_shutdown_drain_is_bounded(tmp_path):
    j = EventJournal(path=tmp_path / "j.jsonl", flush_interval=0.05)
    for i in range(10):
        j.emit(CAT_MOTION, "drive", "ai")
    j.flush_and_close(deadline_s=2.0)
    assert j.health()["writer_alive"] is False


def test_persistence_failure_is_surfaced_not_raised(tmp_path):
    errs = []
    j = EventJournal(path=tmp_path / "j.jsonl", flush_interval=0.05, on_error=errs.append)
    class _Broken:
        def write(self, *_a): raise OSError("disk full")
        def flush(self): raise OSError("disk full")
        def tell(self): return 0
        def close(self): pass
    with j._lock:
        j._fh = _Broken()
    j.emit(CAT_MOTION, "drive", "ai")                    # must NOT raise
    _drain(j, timeout=1.0)
    assert j.persist_failed >= 1 and j.persist_ok is False and errs
    j.flush_and_close()


def test_restart_recovery_across_rotated_files(tmp_path):
    p = tmp_path / "j.jsonl"
    j1 = EventJournal(path=p, max_bytes=400, max_files=3, flush_interval=0.05)
    for i in range(120):
        j1.emit(CAT_MOTION, "drive", "ai", detail={"i": i, "pad": "x" * 40})
    _drain(j1)
    j1.flush_and_close()
    assert list(tmp_path.glob("j.jsonl*"))               # rotated files exist
    # a NEW journal recovers retained history into the ring + continues the sequence
    j2 = EventJournal(path=p, max_bytes=400, max_files=3)
    try:
        assert j2.recovered > 0
        assert j2.health()["newest_ts"] is not None
        nxt = j2.emit(CAT_MOTION, "drive", "ai")
        assert nxt.seq > j2.recovered                    # sequence continued past recovered history
    finally:
        j2.flush_and_close()


def test_cursor_unique_across_process_sessions(tmp_path):
    j1 = EventJournal(path=None)
    c1 = encode_cursor(j1.process_session_id, 5)
    j2 = EventJournal(path=None)
    c2 = encode_cursor(j2.process_session_id, 5)
    assert c1 != c2                                       # same seq, different session -> different opaque cursor
    assert decode_cursor(c1) == 5 and decode_cursor(c2) == 5
    assert decode_cursor("garbage") is None
    j1.flush_and_close(); j2.flush_and_close()


def test_query_filter_pagination_and_order():
    j = EventJournal(path=None, max_mem=1000)
    try:
        for i in range(10):
            j.emit(CAT_MOTION, "drive", "ai", outcome=("moved" if i % 2 else "blocked"), correlation_id="trace")
        j.emit(CAT_SAFETY_TRANSITION, "master_stop", "api", correlation_id="trace", incident_id="inc9")
        moved = j.query(category=CAT_MOTION, outcome="moved")
        assert len(moved["events"]) == 5
        page = j.query(limit=4, order="asc")
        assert page["returned"] == 4 and page["more"] is True
        page2 = j.query(limit=4, order="asc", cursor=page["next_cursor"])
        assert page2["events"][0]["seq"] > page["events"][-1]["seq"]
        assert len(j.correlation_trace("trace")) == 11
        assert j.incident_trace("inc9")[0]["incident_id"] == "inc9"
    finally:
        j.flush_and_close()


def test_time_range_and_persistent_query(tmp_path):
    j = EventJournal(path=tmp_path / "j.jsonl", flush_interval=0.05)
    try:
        e1 = j.emit(CAT_MOTION, "a", "ai")
        time.sleep(0.01)
        mid = e1.ts_utc
        time.sleep(0.01)
        j.emit(CAT_MOTION, "b", "ai")
        r = j.query(start=mid, persistent=True, order="asc")
        types = [e["type"] for e in r["events"]]
        assert "b" in types                              # after mid
    finally:
        j.flush_and_close()


def test_incidents_grouping_and_severity():
    j = EventJournal(path=None)
    try:
        j.emit(CAT_SAFETY_TRANSITION, "master_stop", "api", incident_id="i1", outcome="dispatched")
        j.emit(CAT_SAFETY_TRANSITION, "resume", "api", incident_id="i1", outcome="degraded")
        incs = j.incidents()
        assert incs and incs[0]["incident_id"] == "i1" and incs[0]["severity"] == "critical"
        assert incs[0]["count"] == 2
    finally:
        j.flush_and_close()


def _paginate(j, order, limit):
    """Walk every page in `order`, returning the ordered list of seqs and asserting no duplicates."""
    seqs, seen, cursor = [], set(), None
    for _ in range(100):
        r = j.query(order=order, limit=limit, cursor=cursor)
        for e in r["events"]:
            assert e["seq"] not in seen, f"duplicate seq {e['seq']} in {order} pagination"
            seen.add(e["seq"])
            seqs.append(e["seq"])
        if not r["more"]:
            break
        cursor = r["next_cursor"]
    return seqs


def test_ascending_cursor_pagination_three_pages_no_dup_no_omission():
    j = EventJournal(path=None, max_mem=1000)
    try:
        for i in range(10):
            j.emit(CAT_MOTION, "drive", "ai", detail={"i": i})
        seqs = _paginate(j, "asc", 4)                     # 10 events / 4 -> 3 pages
        assert seqs == sorted(seqs)                        # ascending
        assert len(seqs) == 10 and len(set(seqs)) == 10    # no omission, no duplicate
    finally:
        j.flush_and_close()


def test_descending_cursor_pagination_three_pages_no_dup_no_omission():
    j = EventJournal(path=None, max_mem=1000)
    try:
        for i in range(10):
            j.emit(CAT_MOTION, "drive", "ai", detail={"i": i})
        seqs = _paginate(j, "desc", 4)
        assert seqs == sorted(seqs, reverse=True)          # descending walks BACKWARD (the §1.2 fix)
        assert len(seqs) == 10 and len(set(seqs)) == 10    # no omission, no duplicate
    finally:
        j.flush_and_close()


def test_malformed_cursor_raises():
    j = EventJournal(path=None)
    try:
        with pytest.raises(ValueError):
            j.query(cursor="!!!not-a-cursor!!!")
    finally:
        j.flush_and_close()


def test_foreign_session_cursor_paginates_by_durable_seq():
    j = EventJournal(path=None, max_mem=1000)
    try:
        for i in range(6):
            j.emit(CAT_MOTION, "drive", "ai")
        # a cursor minted by a DIFFERENT (prior) process session, but a valid durable seq, still resumes by seq
        foreign = encode_cursor("some-other-session-id", 3)
        r = j.query(order="asc", cursor=foreign)
        assert r["cursor_foreign_session"] is True
        assert all(e["seq"] > 3 for e in r["events"]) and r["events"]                # resumed by durable seq
    finally:
        j.flush_and_close()


def test_recovery_restores_newest_tail_of_large_active_file(tmp_path):
    # §1.3: a large active file must restore its NEWEST rows, not the first ones encountered.
    p = tmp_path / "j.jsonl"
    j1 = EventJournal(path=p, max_bytes=10_000_000, max_files=2, flush_interval=0.05)  # no rotation
    last = None
    for i in range(200):
        last = j1.emit(CAT_MOTION, "drive", "ai", detail={"i": i})
    _drain(j1)
    j1.flush_and_close()
    j2 = EventJournal(path=p, recover_max_events=20, max_mem=40)
    try:
        drives = [r for r in j2.recent(50) if r["type"] == "drive"]
        assert drives[-1]["seq"] == last.seq               # the NEWEST drive was restored
        assert drives[0]["detail"]["i"] >= 180             # tail (newest ~20), NOT the oldest rows
        # the durable sequence continued from the true max (recovery emits journal_recovered at max+1)
        assert any(r["type"] == "journal_recovered" and r["seq"] == last.seq + 1 for r in j2.recent(50))
        assert j2.emit(CAT_MOTION, "drive", "ai").seq > last.seq
    finally:
        j2.flush_and_close()


def test_full_queue_shutdown_does_not_strand_writer(tmp_path):
    # §1.4: with a full queue (no sentinel can be enqueued) the writer still observes closure, the file is NEVER
    # closed while the writer can still write, and a delayed writer is joined once it can proceed.
    import threading
    j = EventJournal(path=tmp_path / "j.jsonl", queue_max=4, flush_interval=0.05)
    gate = threading.Event()
    orig = j._flush_batch
    def _blocking_flush(batch):
        gate.wait(timeout=5.0)                              # hold the writer inside flush
        return orig(batch)
    j._flush_batch = _blocking_flush                        # type: ignore[assignment]
    for i in range(50):                                     # overflow the queue while the writer is blocked
        j.emit(CAT_MOTION, "drive", "ai", detail={"i": i})
    # close with a short deadline while the writer is still blocked -> it must NOT be force-closed mid-write
    t = threading.Thread(target=lambda: j.flush_and_close(deadline_s=0.3))
    t.start()
    t.join(timeout=3.0)
    assert j._writer.is_alive() is True                     # still blocked; not stranded-and-file-closed
    assert j._fh is not None                                # file NOT closed under a live writer
    gate.set()                                              # let the writer proceed
    j._writer.join(timeout=3.0)
    assert j._writer.is_alive() is False                    # writer observed closure and exited
    assert isinstance(j.undrained, int)                     # exact undrained count recorded
    j.flush_and_close()                                     # idempotent repeat
    assert j.health()["writer_alive"] is False


def test_module_emit_safe_before_configure(monkeypatch):
    monkeypatch.setattr(obs, "_JOURNAL", None)
    assert obs.emit(CAT_MOTION, "drive", "ai") is None


def test_configure_drains_previous(tmp_path):
    j1 = obs.configure(str(tmp_path / "a.jsonl"))
    obs.emit(CAT_MOTION, "drive", "ai")
    j2 = obs.configure(str(tmp_path / "b.jsonl"))
    assert j2 is not j1
    assert j1.health()["writer_alive"] is False           # previous journal drained/closed
    obs.configure(None)
