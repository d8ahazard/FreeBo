"""Live-robot tests — only run with `pytest --hardware` against a running app.

These drive the real robot through the same self-test harness the CLI uses, so the assertion is just "the
harness completed and produced a verdict". Use scripts/robot_selftest.py for the full human-readable report.
"""
from __future__ import annotations

import pytest

from autobot.diagnostics.checks import Options
from autobot.diagnostics.runner import run_selftest


@pytest.mark.hardware
async def test_live_selftest_readonly(app_url):
    """Connection + video + VSLAM only; no driving, talk, or hearing. Returns 0 (all pass) or 1 (a fail)."""
    opts = Options(allow_move=False, test_talk=False, test_hear=False, on_progress=lambda _m: None)
    code = await run_selftest(app_url, opts, only=["connection", "video", "vslam"],
                              json_out=False, color=False)
    assert code in (0, 1)


@pytest.mark.hardware
async def test_live_selftest_with_motion(app_url):
    """Full motion suite (short, clamped bursts; the runner always e-stops afterward)."""
    opts = Options(allow_move=True, test_talk=False, test_hear=False, on_progress=lambda _m: None)
    code = await run_selftest(app_url, opts,
                              only=["connection", "video", "eyes", "move", "rotate", "autonomy", "vslam"],
                              json_out=False, color=False)
    assert code in (0, 1)
