"""Latency metrics recorder: percentiles, timer context manager, snapshot/summary, and JSONL export."""
from __future__ import annotations

import json
import time

from autobot.brain.metrics import Metrics, _percentile


def test_percentile_nearest_rank():
    vals = [float(i) for i in range(1, 101)]  # 1..100
    assert _percentile(vals, 50) == 50
    assert _percentile(vals, 95) == 95
    assert _percentile(vals, 99) == 99
    assert _percentile([], 50) == 0.0
    assert _percentile([7.0], 95) == 7.0


def test_record_and_snapshot_stats():
    m = Metrics()
    for v in (10, 20, 30, 40, 50):
        m.record("phase", v)
    snap = m.snapshot()
    assert set(snap.keys()) == {"phase"}
    st = snap["phase"]
    assert st["count"] == 5
    assert st["max"] == 50.0
    assert st["last"] == 50.0
    assert st["mean"] == 30.0
    assert st["p50"] == 30.0


def test_window_bounds_samples():
    m = Metrics(window=8)
    for i in range(100):
        m.record("p", i)
    assert m.snapshot()["p"]["count"] == 8


def test_timer_records_a_sample():
    m = Metrics()
    with m.timer("blk"):
        time.sleep(0.005)
    st = m.snapshot()["blk"]
    assert st["count"] == 1
    assert st["last"] >= 4.0  # ~5ms slept; allow scheduling slack


def test_summary_is_compact():
    m = Metrics()
    m.record("reason", 12.0)
    s = m.summary()
    assert s["reason"] == {"count": 1, "p50": 12.0, "p95": 12.0}


def test_jsonl_export(tmp_path):
    log = tmp_path / "metrics.jsonl"
    m = Metrics(log_path=str(log))
    m.record("provider", 3.5)
    m.record("provider", 4.5)
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["phase"] == "provider" and rec["ms"] == 3.5 and "ts" in rec


def test_record_is_fail_soft_on_bad_value():
    m = Metrics()
    m.record("x", float("nan"))  # should not raise; nan is stored but stats must not throw
    _ = m.snapshot()
