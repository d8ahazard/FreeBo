"""P0.1 — unified speech path: the actual `say` tool produces a cancellable, sanitized clip."""
from __future__ import annotations

import autobot.brain.audio_state as ast
from autobot.brain.safety import SafetyFloor
from autobot.brain.skills.base import SkillContext
from autobot.brain.skills.core import CoreSkill
from autobot.brain.speech import SpeechService
from autobot.config import Settings
from autobot.robot.link import RobotLink


class _SpeakLink(RobotLink):
    def __init__(self):
        self.queued = []
        self.cancelled = []
        self._pid = 0

    async def info(self): return {}
    async def telemetry(self): return {"awake": True}
    async def snapshot(self): return b"x", None
    async def drive(self, ly, rx): return {"ok": True}
    async def move(self, ly, rx, duration): return {"ok": True}
    async def stop(self): return {"ok": True}
    async def action(self, name): return {"ok": True}
    async def connection(self, state): return {"ok": True}
    async def say_audio(self, g711, codec="mulaw"): return {"ok": True}
    async def say_text(self, text): return {"ok": True}

    async def publish_speech(self, wav):
        self._pid += 1
        self.queued.append(self._pid)
        return {"ok": True, "playback_id": self._pid, "available": True}

    def cancel_playback(self, pid=None):
        self.cancelled.append(pid)
        return {"ok": True}


async def _noop(_ev):
    return None


async def test_say_tool_clip_is_sanitized_and_cancellable(monkeypatch):
    ast.cancel()   # clean slate
    monkeypatch.setattr("autobot.brain.tts.render_wav", lambda text, **k: b"RIFFfakewav")

    link = _SpeakLink()
    settings = Settings(); settings.update(talk_enabled=True)
    svc = SpeechService(link, settings, emit=_noop)
    ctx = SkillContext(link=link, settings=settings, safety=SafetyFloor(), memory=None, identity=None,
                       emit=_noop, speech=svc)
    core = CoreSkill(); core.ctx = ctx

    # Execute the ACTUAL say tool with text containing reserved barge-in words.
    res = await core._say({"text": "Okay, I'll stop and be quiet now — hello there!"})
    assert res["ok"] is True
    assert link.queued == [1]                       # a clip was queued + its playback id retained
    assert svc.active_playback_id == 1
    # Outbound text was sanitized so the robot can't self-trigger barge-in.
    assert "stop" not in ast.current_text().lower() and "quiet" not in ast.current_text().lower()
    assert ast.is_speaking()

    # Inject a STOP (barge-in / emergency): the registered canceller flushes THIS clip.
    ast.cancel()
    assert link.cancelled == [1]
    assert not ast.is_speaking()


async def test_say_tool_respects_talk_gate(monkeypatch):
    ast.cancel()
    link = _SpeakLink()
    settings = Settings(); settings.update(talk_enabled=False)   # talk OFF
    svc = SpeechService(link, settings, emit=_noop)
    ctx = SkillContext(link=link, settings=settings, safety=SafetyFloor(), memory=None, identity=None,
                       emit=_noop, speech=svc)
    core = CoreSkill(); core.ctx = ctx
    res = await core._say({"text": "hello"})
    assert res["ok"] is False and "blocked" in res
    assert link.queued == []
