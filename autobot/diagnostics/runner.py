"""Self-test runner: order the checks, manage settings, and always leave the robot stopped.

Safety contract: whatever the checks toggle (talk, audio-in, autonomy, motion), the runner restores the
original settings and then issues an emergency stop — so a self-test never leaves the robot driving or the
config changed. This is why it's safe to run against the live robot.

`selftest()` is the structured core (returns a dict; used by the CLI and the /api/selftest endpoint).
`run_selftest()` wraps it with the terminal table + exit code.
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Optional

from .checks import ALL_CHECKS, CheckResult, Options, Status
from .client import AppClient

# Settings the checks may flip; we snapshot + restore exactly these.
_MANAGED_KEYS = ("talk_enabled", "allow_audio_in", "autonomy", "allow_think", "allow_motion", "allow_video")

_COLOR = {"PASS": "\033[32m", "FAIL": "\033[31m", "WARN": "\033[33m", "SKIP": "\033[90m"}
_RESET = "\033[0m"


def _c(status: str, text: str, color: bool) -> str:
    return f"{_COLOR.get(status, '')}{text}{_RESET}" if color else text


async def selftest(base_url: str, opts: Options, *, only: Optional[list[str]] = None,
                   skip: Optional[list[str]] = None,
                   on_start: Optional[Callable[[str, str], None]] = None,
                   on_result: Optional[Callable[[CheckResult], None]] = None) -> dict:
    """Run the selected checks against the app at `base_url`; restore settings + e-stop afterward.

    Returns {reachable, results: [CheckResult.to_dict()], summary: {counts}, exit_code}. `on_start(id, desc)`
    and `on_result(CheckResult)` are optional live callbacks (the CLI uses them to print as it goes)."""
    specs = [s for s in ALL_CHECKS
             if (not only or s.id in only) and (not skip or s.id not in skip)]
    results: list[CheckResult] = []
    original: dict = {}

    async with AppClient(base_url) as c:
        if not await c.ping():
            return {"reachable": False, "results": [], "summary": {}, "exit_code": 2,
                    "error": f"cannot reach the app at {base_url}"}

        try:
            st = await c.state()
            cur = st.get("settings", {})
            original = {k: cur.get(k) for k in _MANAGED_KEYS}
        except Exception:  # noqa: BLE001
            original = {}

        try:
            for spec in specs:
                if spec.needs:
                    try:
                        await c.settings(**spec.needs)
                        await asyncio.sleep(0.2)
                    except Exception:  # noqa: BLE001
                        pass
                if on_start:
                    on_start(spec.id, spec.desc)
                try:
                    res = await spec.fn(c, opts)
                except Exception as e:  # noqa: BLE001
                    res = CheckResult(spec.id, Status.FAIL, f"check crashed: {type(e).__name__}: {e}",
                                      hint="harness bug or unexpected app response")
                results.append(res)
                if on_result:
                    on_result(res)
        finally:
            # Always: restore settings, then hard-stop (estop forces autonomy=manual — the safe end state).
            if original:
                with _suppress():
                    await c.settings(**{k: v for k, v in original.items() if v is not None})
            with _suppress():
                await c.estop()

    counts = {s: 0 for s in (Status.PASS, Status.FAIL, Status.WARN, Status.SKIP)}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {"reachable": True, "results": [r.to_dict() for r in results], "summary": counts,
            "exit_code": 1 if counts.get(Status.FAIL) else 0}


async def run_selftest(base_url: str, opts: Options, *, only: Optional[list[str]] = None,
                       skip: Optional[list[str]] = None, json_out: bool = False, color: bool = True) -> int:
    """CLI wrapper: prints a live table + summary, returns a process exit code (0 = no failures)."""
    def out(msg: str = "") -> None:
        print(msg, flush=True)

    out("")
    out(f"FreeBo capability self-test  →  {base_url}")
    out(f"move={'on' if opts.allow_move else 'off'}  talk={'on' if opts.test_talk else 'off'}  "
        f"hear={'on' if opts.test_hear else 'off'}")
    out("-" * 72)

    def on_start(cid: str, desc: str) -> None:
        out(f"[{cid}] {desc} …")

    def on_result(r: CheckResult) -> None:
        sym = Status.SYMBOL.get(r.status, "?")
        out(f"  {_c(r.status, sym + ' ' + r.status, color)}  {r.detail}  ({r.elapsed:.1f}s)")
        if r.status in (Status.FAIL, Status.WARN) and r.hint:
            out(f"      → {r.hint}")

    report = await selftest(base_url, opts, only=only, skip=skip, on_start=on_start, on_result=on_result)
    if not report.get("reachable"):
        out(report.get("error", "app unreachable"))
        return report.get("exit_code", 2)

    out("-" * 72)
    out("Restored your settings and issued an emergency stop "
        "(autonomy is now MANUAL — re-enable auto in the UI when ready).")

    s = report["summary"]
    out("")
    out("SUMMARY")
    for r in report["results"]:
        sym = Status.SYMBOL.get(r["status"], "?")
        out(f"  {_c(r['status'], sym, color)} {r['name']:<12} {r['status']:<5} {r['detail']}")
    out("")
    out(f"  {s.get('PASS', 0)} pass   {s.get('FAIL', 0)} fail   "
        f"{s.get('WARN', 0)} warn   {s.get('SKIP', 0)} skip")
    if json_out:
        out(json.dumps(report, indent=2))
    return report["exit_code"]


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True   # swallow everything in cleanup


def _make_ask(interactive: bool):
    if not interactive:
        return None

    async def ask(prompt: str) -> None:
        await asyncio.to_thread(input, prompt)
    return ask


__all__ = ["selftest", "run_selftest", "Options"]
