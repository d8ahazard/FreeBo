"""agent_next_2 §10 — static authority audit.

Scans PRODUCTION code (the `autobot/` package, excluding tests) and FAILS on patterns that would re-introduce a
robot-effect authority bypass six commits later: fire-and-forget `rtm._send(`, physical-effect `raw(` calls,
and `sent_to_agora=True` stamped on a local-only state mutation. The allowlist is small and documented here.

This is a guardrail, not a substitute for the runtime tests — it just stops the bypasses from quietly returning.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "autobot"

# The ONLY module allowed to call the private sidecar transport (`_send`) is the RtmNode implementation.
_SEND_ALLOWED = {"robot/rtm_node.py"}
# `raw(` is allowed only where: rtm_node.py defines/forwards it, and air2_native_link.py uses it SOLELY for the
# audio call-mode handshake ids (102001/102003) — never a physical-effect id.
_RAW_ALLOWED = {"robot/rtm_node.py", "robot/air2_native_link.py"}
_AUDIO_RAW_IDS = {"102001", "102003"}


def _py_files():
    for p in PKG.rglob("*.py"):
        yield p, p.relative_to(PKG).as_posix()


def test_no_rtm_private_send_outside_rtm_node():
    offenders = []
    for path, rel in _py_files():
        if rel in _SEND_ALLOWED:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "rtm._send(" in line:
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "fire-and-forget rtm._send() outside rtm_node.py:\n" + "\n".join(offenders)


def test_no_physical_effect_raw_calls():
    offenders = []
    raw_re = re.compile(r"\.raw\(\s*([0-9]+)")
    for path, rel in _py_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in raw_re.finditer(line):
                rid = m.group(1)
                if rel in _RAW_ALLOWED and rid in _AUDIO_RAW_IDS:
                    continue                       # audio call-mode handshake is allowed
                offenders.append(f"{rel}:{i}: raw({rid}) {line.strip()}")
            # a bare `.raw(` with a non-numeric/variable id outside the allowlist is also suspicious
            if ".raw(" in line and not raw_re.search(line) and rel not in _RAW_ALLOWED:
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "physical-effect raw() calls (only audio 102001/102003 in the allowlist):\n" + \
        "\n".join(offenders)


def test_no_sent_to_agora_true_on_local_mutations():
    # `sent_to_agora=True` must never be claimed by Python on a local-only state mutation (RESET/reconcile).
    offenders = []
    pat = re.compile(r"sent_to_agora\"?\s*[:=]\s*True")
    for path, rel in _py_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "sent_to_agora=True stamped in Python (local mutation must not claim an SDK send):\n" + \
        "\n".join(offenders)


def test_air2_motion_requires_a_ticket():
    # air2 move()/drive() must reject a missing motion ticket (no fallback to current RtmNode state).
    src = (PKG / "robot" / "air2_native_link.py").read_text(encoding="utf-8")
    assert "_ticketed(" not in src, "the un-ticketed motion fallback (_ticketed) must be removed"
    assert "missing_motion_ticket" in src, "air2 move/drive must reject a missing ticket"
