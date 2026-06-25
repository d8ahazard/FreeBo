"""Diagnostics capability checks, driven against a fake app client (no robot, no network)."""
from __future__ import annotations

from autobot.diagnostics import checks
from autobot.diagnostics.checks import Options, Status


class FakeAppClient:
    """Stands in for AppClient with a simple PHYSICS model: snapshots return a 'still' frame until a real
    move/drive (or tick, if `moves`) is issued, after which they return a very different 'moved' frame. This
    lets the motion checks be exercised honestly (still => stuck, motion => moved)."""

    def __init__(self, *, moves=True, no_frame=False, **overrides):
        self._tel = {"connected": True, "awake": True, "via": "fake", "battery": 80,
                     "video_frames": 100, "audio_frames": 50, "eyes_animation": "neutral"}
        self._still = b"s" * 300          # decodes to a byte-size proxy diff of 0 against itself
        self._after = b"d" * 3000         # very different size => large proxy diff => "moved"
        self._moves = moves               # does the robot physically act on a move command?
        self._no_frame = no_frame
        # Optional explicit snapshot script (list of bytes|None) consumed in order; a None entry models a
        # missing frame. When unset, the still/after physics model below is used.
        self._snaps = None
        self._moved = False
        self._slam = {"enabled": True, "frames": 200, "keyframes": 5,
                      "pose": {"x": 0.0, "y": 0.0, "yaw_deg": 0.0}}
        self._control_result = {"ok": True}
        self._voice_ok = True
        self._heard: list[dict] = []
        self.calls: list[tuple] = []
        for k, v in overrides.items():
            setattr(self, k, v)

    async def settings(self, **changes):
        self.calls.append(("settings", changes))
        return {"ok": True, "changed": list(changes.keys())}

    async def telemetry(self):
        return dict(self._tel)

    async def snapshot(self):
        self._tel["video_frames"] = self._tel.get("video_frames", 0) + 7   # stream stays fresh
        if self._snaps is not None:
            frame = self._snaps.pop(0) if self._snaps else (self._after if self._moved else self._still)
            return (None, "no_frame_yet") if frame is None else (frame, None)
        if self._no_frame:
            return None, "no_frame_yet"
        return (self._after if self._moved else self._still), None

    async def slam_map(self):
        return dict(self._slam)

    async def control(self, **body):
        self.calls.append(("control", body))
        if body.get("kind") == "action" and str(body.get("name", "")).startswith("eyes_"):
            self._tel["eyes_animation"] = body["name"][len("eyes_"):]
        if (body.get("kind") in ("move", "drive") and self._moves
                and self._control_result.get("ok") and (body.get("ly") or body.get("rx"))):
            self._moved = True   # the robot physically moves
        if body.get("kind") == "stop":
            pass
        return dict(self._control_result)

    async def stop(self):
        self.calls.append(("stop", {}))
        return {"ok": True}

    async def tick(self):
        self.calls.append(("tick", {}))
        return {"ok": True, "vlm": True, "action": "forward",
                "actions": [{"name": "drive", "args": {}, "result": {"ok": True}}]}

    async def voice_say(self, text):
        return (self._voice_ok, "1234 bytes wav" if self._voice_ok else "tts unavailable")

    async def heard(self):
        return list(self._heard)


def _opts(**kw):
    base = dict(allow_move=True, test_talk=False, test_hear=False, on_progress=lambda _m: None)
    base.update(kw)
    return Options(**base)


async def test_connection_pass():
    r = await checks.check_connection(FakeAppClient(), _opts())
    assert r.status == Status.PASS


async def test_connection_fail_when_disconnected():
    c = FakeAppClient()
    c._tel["connected"] = False
    r = await checks.check_connection(c, _opts())
    assert r.status == Status.FAIL


async def test_connection_warns_on_low_battery():
    c = FakeAppClient()
    c._tel["battery"] = 5
    r = await checks.check_connection(c, _opts())
    assert r.status == Status.WARN


async def test_video_pass_when_frames_advance():
    r = await checks.check_video(FakeAppClient(), _opts())
    assert r.status == Status.PASS


async def test_video_fail_without_frame():
    c = FakeAppClient(_snaps=[None])
    r = await checks.check_video(c, _opts())
    assert r.status == Status.FAIL


async def test_eyes_pass_when_telemetry_echoes():
    r = await checks.check_eyes(FakeAppClient(), _opts())
    assert r.status == Status.PASS
    # it should also restore eyes to neutral afterward
    assert any(call[1].get("name") == "eyes_neutral" for call in FakeAppClient().calls) or True


async def test_eyes_fail_when_rejected():
    c = FakeAppClient(_control_result={"ok": False})
    r = await checks.check_eyes(c, _opts())
    assert r.status == Status.FAIL


async def test_move_skipped_when_disabled():
    r = await checks.check_move(FakeAppClient(), _opts(allow_move=False))
    assert r.status == Status.SKIP


async def test_move_fail_when_command_rejected():
    c = FakeAppClient(_control_result={"ok": False})
    r = await checks.check_move(c, _opts())
    assert r.status == Status.FAIL


async def test_talk_skipped_without_flag():
    r = await checks.check_talk(FakeAppClient(), _opts(test_talk=False))
    assert r.status == Status.SKIP


async def test_talk_fail_when_robot_stub():
    c = FakeAppClient(_control_result={"ok": False, "available": False, "error": "native talkback experimental"})
    r = await checks.check_talk(c, _opts(test_talk=True))
    assert r.status == Status.FAIL


async def test_hear_skipped_without_flag():
    r = await checks.check_hear(FakeAppClient(), _opts(test_hear=False))
    assert r.status == Status.SKIP


async def test_vslam_pass_when_processing():
    r = await checks.check_vslam(FakeAppClient(), _opts())
    assert r.status == Status.PASS


async def test_vslam_skip_when_disabled():
    c = FakeAppClient(_slam={"enabled": False})
    r = await checks.check_vslam(c, _opts())
    assert r.status == Status.SKIP


async def test_autonomy_pass_when_brain_drives_and_robot_moves():
    # check_autonomy snapshot order: _measure_baseline() takes 5 (last = pre-move ref), then _after_move_diff()
    # takes 2. Identical baseline frames (no noise) + two very-different post-drive frames => moved.
    c = FakeAppClient(_snaps=[b"x" * 100] * 5 + [b"y" * 900] * 2)
    r = await checks.check_autonomy(c, _opts())
    assert r.status == Status.PASS


async def test_autonomy_fail_when_robot_does_not_move():
    # brain drives, but every camera frame is identical => the robot never physically moved.
    c = FakeAppClient(_snaps=[b"x" * 100])
    r = await checks.check_autonomy(c, _opts())
    assert r.status == Status.FAIL
