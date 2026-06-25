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
    async def drive(self, ly, rx, *, generation=None, epoch=None): return {"ok": True}
    async def move(self, ly, rx, duration, *, generation=None, epoch=None): return {"ok": True}
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


class _FailLink(_SpeakLink):
    async def publish_speech(self, wav):
        return {"ok": False, "error": "channel not ready"}


class _NoPidLink(_SpeakLink):
    async def publish_speech(self, wav):   # cancellable link but no playback id -> uncancellable -> failure
        return {"ok": True}


async def _noop(_ev):
    return None


def _svc(monkeypatch, link, talk=True):
    monkeypatch.setattr("autobot.brain.tts.render_wav", lambda text, **k: b"RIFFfakewav")
    settings = Settings(); settings.update(talk_enabled=talk)
    return SpeechService(link, settings, emit=_noop)


async def test_publish_ok_false_clears_gate_immediately(monkeypatch):
    ast.cancel()
    svc = _svc(monkeypatch, _FailLink())
    res = await svc.speak("hello there")
    assert res["ok"] is False
    assert svc.active_playback_id is None
    assert not ast.is_speaking()       # gate NOT left open for the computed clip duration


async def test_cancellable_link_without_playback_id_is_failure(monkeypatch):
    ast.cancel()
    svc = _svc(monkeypatch, _NoPidLink())
    res = await svc.speak("hello there")
    assert res["ok"] is False and not ast.is_speaking() and svc.active_playback_id is None


async def test_two_rapid_says_cancel_the_first(monkeypatch):
    ast.cancel()
    link = _SpeakLink()
    svc = _svc(monkeypatch, link)
    await svc.speak("first clip")
    await svc.speak("second clip")
    assert link.queued == [1, 2]
    assert 1 in link.cancelled          # the first clip was cancelled before the second published
    assert svc.active_playback_id == 2


async def test_stop_while_first_clip_audible(monkeypatch):
    ast.cancel()
    link = _SpeakLink()
    svc = _svc(monkeypatch, link)
    await svc.speak("a long clip that is playing")
    assert ast.is_speaking() and link.queued == [1]
    ast.cancel()                        # barge-in STOP
    assert link.cancelled == [1] and not ast.is_speaking()


async def test_stale_completion_from_old_clip_does_not_clear_new(monkeypatch):
    ast.cancel()
    link = _SpeakLink()
    svc = _svc(monkeypatch, link)
    await svc.speak("clip A")
    gen_a = svc._gen
    await svc.speak("clip B")           # B is now the active generation
    # A's delayed completion fires late -> must NOT clear B's gate.
    ast.clear(gen_a)
    assert ast.is_speaking()
    assert "b" in ast.current_text().lower()   # still clip B's text


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
