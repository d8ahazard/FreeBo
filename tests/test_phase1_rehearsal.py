"""agent_next_4 §7/§9 — guards for the software-only R4.0 rehearsal: the ordered-subsequence trace matcher and
the journal-pressure/restart-recovery scenarios (the parts that need neither Node nor the heavy model imports).
The full 12-scenario rehearsal runs as its own command: `python scripts/phase1_rehearsal.py`."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "phase1_rehearsal", Path(__file__).resolve().parents[1] / "scripts" / "phase1_rehearsal.py")
reh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reh)  # type: ignore[union-attr]


def test_ordered_subsequence_matcher():
    assert reh._ordered(["a", "x", "b", "y", "c"], ["a", "b", "c"]) is True
    assert reh._ordered(["a", "c", "b"], ["a", "b", "c"]) is False    # out of order
    assert reh._ordered(["a", "b"], ["a", "b", "c"]) is False         # missing tail
    assert reh._ordered([], []) is True


def test_journal_scenarios_trace_complete(tmp_path, monkeypatch):
    # Point the rehearsal evidence dir at tmp so the test never writes into the repo tree.
    r = reh.Rehearsal.__new__(reh.Rehearsal)
    from autobot import observability as obs
    r.sha = "test"
    r.out_dir = tmp_path
    r.journal_path = tmp_path / "events.jsonl"
    r.j = obs.configure(str(r.journal_path), flush_interval=0.05)
    r.results = []
    try:
        r.run_journal_scenarios()
        names = {x["scenario"]: x for x in r.results}
        assert names["11 journal queue pressure + persistence failure"]["ok"] is True
        assert names["12 clean shutdown + restart recovery"]["ok"] is True
    finally:
        obs.configure(None)
