"""Brain: the VLM reason path drives through the single ActionExecutor (mocked vision service, MockRobotLink).

Motion confirmation itself lives in the ActionExecutor now (see test_action_executor.py); the old
MotionConfirmer was retired in Phase 0.7.
"""
from __future__ import annotations

import pytest
from conftest import settings  # noqa: F401  (kept for parity with the rest of the suite)

from autobot.robot.mock_link import MockRobotLink


@pytest.fixture(autouse=True)
def _no_ffmpeg(monkeypatch):
    # Force the mock's instant solid-JPEG fallback so executor evidence polling is fast + deterministic.
    monkeypatch.setattr("autobot.robot.mock_link.shutil.which", lambda _x: None)


async def test_brain_vlm_tick_drives_through_executor(tmp_path, monkeypatch):
    import autobot.brain.vlm_client as vc
    from autobot.brain.agent import AgentBrain
    from autobot.brain.identity import Identity
    from autobot.brain.memory import Memory
    from autobot.config import SETTINGS

    monkeypatch.setenv("AUTOBOT_AI_PROVIDER", "vlm")   # force the modular vision brain (legacy env trigger)
    # Phase 0 default is calm-observe (scope=adjust, rotate-only). This test verifies the VLM "forward"
    # decision actually drives, so opt into active roaming explicitly (must be set BEFORE the brain — and its
    # BehaviorController — is constructed). See docs/MOTION.md (Phase 0 acceptance gate).
    monkeypatch.setenv("AUTOBOT_ACTIVE_EXPLORE", "1")
    SETTINGS.update(setup_complete=True, autonomy="auto", allow_motion=True, allow_think=True,
                    allow_video=True, talk_enabled=False, confirm_motion=True, ai_provider="vlm")

    async def fake_decide(self, **kwargs):
        return {"action": "forward", "text": "", "eyes": "curious"}

    monkeypatch.setattr(vc.VlmClient, "decide", fake_decide)
    assert vc.vlm_enabled()

    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    link = MockRobotLink()
    brain = AgentBrain(SETTINGS, emit, link, Memory(base_dir=str(tmp_path / "mem")),
                       Identity(emit=lambda _ev: None))
    # keep the executor fast for the test
    brain.executor.evidence_timeout = 0.4
    brain.executor.settle = 0.02
    brain.executor.poll = 0.01

    res = await brain.tick(force=True)
    assert res.get("ok") and res.get("vlm") and res.get("action") == "forward"
    # The brain dispatched a drive (intent) and an ActionExecutor lifecycle event was emitted.
    drove = [e for e in events if e.get("type") == "tool_call" and e.get("name") == "drive"]
    assert drove, "the brain dispatched a drive"
    actions = [e for e in events if e.get("type") == "action"]
    assert actions and any(e.get("state") in ("succeeded", "unknown", "failed") for e in actions)
    assert brain.status_dict()["motion_state"] in (None, "moved", "stuck", "blocked", "unknown")
