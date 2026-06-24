"""Phase 0.9 — video-rate visual reflex: cheap callback + dedicated worker + loop-handoff firing."""
from __future__ import annotations

import threading
import time
import types

import pytest

from autobot.brain.reflex_vision import LoomingDetector, VisualReflex
from autobot.robot.media_hub import MediaHub


def _frame():
    return types.SimpleNamespace(wall_ts=time.monotonic(), seq=1, gray=lambda: "G")


def test_callback_only_enqueues_no_processing():
    rf = VisualReflex(on_loom=lambda s: None, threshold=0.1)
    rf._det.update_gray = lambda g: 0.9          # would fire IF the callback processed (it must not)
    rf._on_frame(_frame())
    assert rf._pending is not None and rf.fires == 0   # callback only stored the frame + returned


def test_visual_reflex_fires_via_worker():
    hub = MediaHub()
    got = {}
    done = threading.Event()

    def on_loom(score):
        got["score"] = score
        done.set()

    rf = VisualReflex(on_loom=on_loom, threshold=0.1)
    rf._det.update_gray = lambda g: 0.5          # force a looming detection in the worker
    rf.attach(hub)
    try:
        hub.publish_video(_frame())
        assert done.wait(2.0)
        assert got["score"] == 0.5 and rf.fires == 1 and rf.last_latency_ms >= 0.0
    finally:
        rf.stop()


def test_visual_reflex_drops_superseded_frames():
    hub = MediaHub()
    seen = []
    ev = threading.Event()

    def on_loom(score):
        seen.append(score)
        ev.set()

    rf = VisualReflex(on_loom=on_loom, threshold=0.1)
    rf._det.update_gray = lambda g: 0.3
    # don't attach the worker; verify the bounded single-slot holder keeps only the newest frame
    rf._on_frame(types.SimpleNamespace(wall_ts=1.0, seq=1, gray=lambda: "A"))
    rf._on_frame(types.SimpleNamespace(wall_ts=2.0, seq=2, gray=lambda: "B"))
    frame, _arrival = rf._pending
    assert frame.seq == 2          # newest only; the older frame was superseded


def test_looming_still_when_no_motion():
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    d = LoomingDetector()
    g = np.zeros((120, 160), dtype=np.uint8)
    assert d.update_gray(g) == 0.0     # first frame: no previous
    assert d.update_gray(g) == 0.0     # identical view: no expansion -> not looming
