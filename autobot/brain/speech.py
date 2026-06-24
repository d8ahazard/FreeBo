"""SpeechService — the ONE robot-speaker path, shared by the agent's reflex speech and the cortex `say` tool.

Before this, `AgentBrain._speak()` and `CoreSkill._say()` each rendered + published TTS differently — only the
former sanitized reserved words, recorded the outbound text, retained the playback id, and registered a
canceller. That meant a STOP/QUIET could not reliably cancel a clip spoken via the `say` tool. Every utterance
now goes through `speak()`, which uniformly:

  1. sanitizes reserved STOP/QUIET phrases out of the outbound text (so the robot never self-triggers barge-in),
  2. records the ACTUAL outbound text in AudioState (echo gate + barge-in self-echo rejection),
  3. obtains and retains the playback id from the link,
  4. registers an idempotent canceller for that id (so AudioState.cancel() flushes the clip),
  5. clears the speaking state when playback completes or fails.

Pure-ish: holds only `link`, the live `settings`, and `emit`. No agent import.
"""
from __future__ import annotations

import asyncio
import base64
import time
from typing import Awaitable, Callable, Optional


class SpeechService:
    def __init__(self, link, settings, emit: Optional[Callable[[dict], Awaitable[None]]] = None) -> None:
        self.link = link
        self.settings = settings
        self.emit = emit
        self.last_spoken = ""
        self.active_playback_id = None

    async def _emit(self, ev: dict) -> None:
        if self.emit:
            try:
                await self.emit(ev)
            except Exception:  # noqa: BLE001
                pass

    def _schedule_clear(self, pid, dur: float) -> None:
        """Clear the retained playback id once the clip should have finished (completion), unless a newer clip
        replaced it. The AudioState echo gate auto-expires on its own timer; this just drops the stale id."""
        from . import audio_state

        async def _clr():
            try:
                await asyncio.sleep(max(0.0, dur) + audio_state.TAIL_S)
            except asyncio.CancelledError:
                return
            if self.active_playback_id == pid:
                self.active_playback_id = None
        try:
            asyncio.create_task(_clr())
        except RuntimeError:
            pass   # no running loop (non-async caller) — the gate timer still clears the speaking state

    async def speak(self, text: str, *, check_say: bool = False, safety=None) -> dict:
        """Render + speak `text` on the robot speaker through the unified path. `check_say` gates on the talk
        toggle / quiet window via `safety` (used by the cortex `say` tool)."""
        from . import audio_state, critical_words, tts
        from .speech_clean import clean_spoken

        s = self.settings.snapshot()
        if check_say and safety is not None:
            d = safety.check_say(s)
            if not d.allowed:
                return {"ok": False, "blocked": d.reason}
        text = clean_spoken(str(text or ""))
        # Never UTTER a barge-in trigger word — it would self-trigger the interrupt detector (no hardware AEC).
        text = critical_words.strip_reserved(text)
        if not text:
            return {"ok": False, "error": "empty text"}
        self.last_spoken = text
        try:
            pub = getattr(self.link, "publish_speech", None)
            if callable(pub):
                wav = tts.render_wav(text)
                if not wav:
                    return self._as_dict(await self.link.say_text(text))
                dur = audio_state.wav_duration_s(wav)
                audio_state.mark_speaking(dur, text=text)   # echo gate ON + record the outbound text
                res = await pub(wav)
                pid = res.get("playback_id") if isinstance(res, dict) else None
                self.active_playback_id = pid
                cancel = getattr(self.link, "cancel_playback", None)
                if cancel is not None:
                    # cancel_playback is sync + thread-safe; register it so AudioState.cancel() flushes the clip.
                    audio_state.mark_speaking(dur, text=text,
                                              canceller=lambda pid=pid, cancel=cancel: cancel(pid))
                await self._emit({"type": "speech", "text": text, "b64": base64.b64encode(wav).decode(),
                                  "sr": 0, "ts": time.time()})
                self._schedule_clear(pid, dur)
                return self._as_dict(res)
            if self.link.prefers_text_tts():
                return self._as_dict(await self.link.say_text(text))
            g711 = tts.render_mulaw(text)
            if not g711:
                return self._as_dict(await self.link.say_text(text))
            audio_state.mark_speaking(len(g711) / 8000.0, text=text)   # G.711 @ 8 kHz, 1 byte/sample
            return self._as_dict(await self.link.say_audio(g711, codec="mulaw"))
        except Exception as e:  # noqa: BLE001
            audio_state.cancel()   # clear the speaking state on failure
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _as_dict(res) -> dict:
        return res if isinstance(res, dict) else {"ok": bool(res)}
