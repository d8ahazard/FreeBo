"""Brain: closed-loop motion confirmation + the VLM reason path (mocked vision service, MockRobotLink)."""
from __future__ import annotations

from conftest import settings

from autobot.brain.motion_check import MotionConfirmer
from autobot.robot.mock_link import MockRobotLink, _solid_jpeg


def test_motion_confirmer_stuck_on_identical_frame():
    mc = MotionConfirmer()
    j = _solid_jpeg()
    mc.record(j, ly=0.5, rx=0.0)
    assert mc.has_pending()
    res = mc.confirm(j)
    assert res is not None and res.state == "stuck"
    assert not mc.has_pending()


def test_motion_confirmer_no_pending_returns_none():
    assert MotionConfirmer().confirm(b"x") is None


def test_motion_confirmer_ignores_false_vslam_motion():
    # VSLAM claims a big move, but the camera view is identical -> the robot did NOT move (stuck).
    mc = MotionConfirmer(pose_provider=lambda: {"pose": {"x": 0.0, "y": 0.0, "yaw_deg": 0.0}})
    mc.record(_solid_jpeg(), ly=0.5, rx=0.0)
    mc.pose_provider = lambda: {"pose": {"x": 5.0, "y": 0.0, "yaw_deg": 0.0}}  # drifted pose (false)
    res = mc.confirm(_solid_jpeg())
    assert res.state == "stuck"


async def test_brain_vlm_tick_drives_and_arms_motion_check(tmp_path, monkeypatch):
    import autobot.brain.vlm_client as vc
    from autobot.brain.agent import AgentBrain
    from autobot.brain.identity import Identity
    from autobot.brain.memory import Memory
    from autobot.config import SETTINGS

    monkeypatch.setenv("AUTOBOT_AI_PROVIDER", "vlm")   # force the modular vision brain (legacy env trigger)
    # Brain mode is now settings-driven (the UI is authoritative at runtime); set it on SETTINGS too so the
    # agent's snapshot-based resolution picks the VLM path. See docs/MATURITY.md §1.
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

    res = await brain.tick(force=True)
    assert res.get("ok") and res.get("vlm") and res.get("action") == "forward"
    assert link.state["last_drive"] != (0.0, 0.0)        # the robot was actually told to move
    assert brain.motion.has_pending()                    # closed-loop check armed for the next cycle
    assert brain.status_dict()["motion_state"] in (None, "moved", "stuck", "blocked", "unknown")
