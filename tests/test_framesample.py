"""Phase 0.6 — sequence-aware FrameSample: motion evidence can tell a NEW frame from a stale cached one."""
from __future__ import annotations

import pytest

from autobot.robot.link import RobotLink
from autobot.robot.media_hub import FrameSample, MediaHub, VideoFrame
from autobot.robot.mock_link import MockRobotLink


def _video_frame(seq: int, val: int = 0):
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    import time
    bgr = np.full((16, 16, 3), val % 256, dtype=np.uint8)
    return VideoFrame(bgr=bgr, width=16, height=16, seq=seq, rtp_ts=seq * 3000,
                      wall_ts=time.monotonic())


def test_media_hub_latest_sample_atomic_and_seq():
    hub = MediaHub()
    assert hub.latest_sample().valid is False
    hub.publish_video(_video_frame(1, 10))
    s1 = hub.latest_sample()
    assert s1.valid and s1.seq == 1 and s1.jpeg
    hub.publish_video(_video_frame(2, 200))
    s2 = hub.latest_sample()
    assert s2.seq == 2 and s2.seq > s1.seq


async def test_mock_snapshot_sample_advances_seq():
    link = MockRobotLink()
    a = await link.snapshot_sample()
    b = await link.snapshot_sample()
    assert a.valid and b.valid
    assert b.seq is not None and a.seq is not None and b.seq > a.seq


async def test_mock_freeze_seq_is_stale():
    link = MockRobotLink()
    a = await link.snapshot_sample()
    link._freeze_seq = True
    b = await link.snapshot_sample()
    assert b.seq == a.seq          # stalled stream -> same seq (evidence must read UNKNOWN, not moved/stuck)


async def test_contract_default_wrap_has_no_seq():
    # A link that only implements snapshot() gets the default wrap -> seq is None (freshness unprovable).
    class _Tiny(RobotLink):
        async def info(self): return {}
        async def telemetry(self): return {"awake": True}
        async def snapshot(self): return b"jpegbytes", None
        async def drive(self, ly, rx, *, generation=None, epoch=None, ticket_id=None): return {"ok": True}
        async def move(self, ly, rx, duration, *, generation=None, epoch=None, ticket_id=None): return {"ok": True}
        async def stop(self): return {"ok": True}
        async def action(self, name): return {"ok": True}
        async def connection(self, state): return {"ok": True}
        async def say_audio(self, g711, codec="mulaw"): return {"ok": True}
        async def say_text(self, text): return {"ok": True}

    fs = await _Tiny().snapshot_sample()
    assert isinstance(fs, FrameSample)
    assert fs.valid is True and fs.seq is None and fs.jpeg == b"jpegbytes"


async def test_perceive_attaches_frame():
    from autobot.brain.perception import perceive
    obs = await perceive(MockRobotLink(), want_image=True)
    assert obs.frame is not None and obs.frame.valid
    assert obs.jpeg == obs.frame.jpeg          # jpeg retained as a plain field for compatibility
