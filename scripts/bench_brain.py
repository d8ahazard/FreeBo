#!/usr/bin/env python3
"""Offline brain-latency benchmark (no hardware, no GPU, no network).

Drives N reason cycles against the MockRobotLink with a STUBBED LLM provider, then prints the per-phase
latency table the agent records (see autobot/brain/metrics.py + docs/MATURITY.md §2). Use it to compare
releases on identical traces and to catch per-phase p95 regressions.

    python scripts/bench_brain.py --ticks 50
    python scripts/bench_brain.py --ticks 50 --jsonl data/bench.jsonl   # also dump raw samples

The stub provider returns a `set_eyes` tool call on alternating rounds so each tick exercises the
perceive -> provider -> tool -> provider -> reason path with a small simulated think latency.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fmt_row(name: str, st: dict) -> str:
    return (f"{name:<14} {st['count']:>5}  {st['p50']:>8.2f} {st['p95']:>8.2f} "
            f"{st['p99']:>8.2f} {st['mean']:>8.2f} {st['max']:>8.2f}")


async def run(ticks: int) -> dict:
    from autobot.brain.agent import AgentBrain
    from autobot.brain.identity import Identity
    from autobot.brain.memory import Memory
    from autobot.brain.providers import ChatResult
    from autobot.brain.providers import openai_compatible as oai
    from autobot.config import Settings
    from autobot.robot.mock_link import MockRobotLink

    # A fresh, fully-enabled settings object pointed at a local (keyless) endpoint so the brain is "ready".
    s = Settings()
    s.update(setup_complete=True, autonomy="auto", allow_think=True, allow_motion=True, allow_video=True,
             talk_enabled=False, confirm_motion=False, ai_provider="openai", ai_model="bench-model",
             ai_base_url="http://127.0.0.1:0/v1", ai_api_key="")

    # Stub the provider: alternate a tool call (set_eyes) and a plain reply, with a small simulated latency.
    _round = {"n": 0}

    async def fake_chat(self, messages, tools=None, temperature=0.4):  # noqa: ANN001
        await asyncio.sleep(0.01)
        _round["n"] += 1
        if _round["n"] % 2 == 1:
            return ChatResult(content="looking around",
                              tool_calls=[{"id": "c1", "name": "set_eyes", "arguments": {"animation": "curious"}}])
        return ChatResult(content="all clear", tool_calls=[])

    oai.OpenAICompatibleClient.chat = fake_chat

    link = MockRobotLink()
    tmp = tempfile.mkdtemp(prefix="freebo-bench-")
    brain = AgentBrain(s, _noop_emit, link, Memory(base_dir=os.path.join(tmp, "mem")),
                       Identity(emit=lambda _ev: None))

    # Hide the MockRobotLink's chatty [mock] prints during the run; we only want the table.
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(ticks):
            await brain.tick(force=True)

    return brain.metrics.snapshot()


async def _noop_emit(_ev: dict) -> None:
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline brain-latency benchmark (mock link, stub provider).")
    ap.add_argument("--ticks", type=int, default=30, help="number of reason cycles to run (default 30)")
    ap.add_argument("--jsonl", type=str, default="", help="also append raw samples to this JSONL path")
    args = ap.parse_args()

    if args.jsonl:
        os.environ["AUTOBOT_METRICS_LOG"] = args.jsonl

    snap = asyncio.run(run(max(1, args.ticks)))

    print(f"\nFreeBo brain latency over {args.ticks} ticks (ms)\n")
    print(f"{'phase':<14} {'count':>5}  {'p50':>8} {'p95':>8} {'p99':>8} {'mean':>8} {'max':>8}")
    print("-" * 70)
    # Show the headline cycle first, then the rest alphabetically.
    order = ["reason", "perceive", "provider", "tool"]
    for name in order:
        if name in snap:
            print(_fmt_row(name, snap[name]))
    for name in sorted(snap):
        if name not in order:
            print(_fmt_row(name, snap[name]))
    if args.jsonl:
        print(f"\nraw samples appended to {args.jsonl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
