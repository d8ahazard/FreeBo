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
        self._gen = 0                      # AudioState generation of our current clip
        self._play_lock = asyncio.Lock()   # exactly ONE active robot utterance at a time
        self._clear_tasks: set[asyncio.Task] = set()   # P0 §6: tracked so teardown cancels+awaits them

    async def _emit(self, ev: dict) -> None:
        if self.emit:
            try:
                await self.emit(ev)
            except Exception:  # noqa: BLE001
                pass

    def _schedule_clear(self, gen: int, pid, dur: float) -> None:
        """Clear THIS clip's speaking state once it should have finished (completion). Generation-scoped, so a
        stale completion from an older clip can't clear a newer clip's gate."""
        from . import audio_state

        async def _clr():
            try:
                await asyncio.sleep(max(0.0, dur) + audio_state.TAIL_S)
            except asyncio.CancelledError:
                return
            if self.active_playback_id == pid:
                self.active_playback_id = None
            audio_state.clear(gen)   # no-op if a newer clip is already active
        try:
            # P0 §6: TRACK the task so a teardown can cancel + await it (don't leak a pending task that the
            # loop later destroys -> "Task was destroyed but it is pending!" + intermittent shutdown hangs).
            t = asyncio.create_task(_clr())
            self._clear_tasks.add(t)
            t.add_done_callback(self._clear_tasks.discard)
        except RuntimeError:
            pass   # no running loop (non-async caller) — the gate timer still clears the speaking state

    async def aclose(self) -> None:
        """Cancel + await any in-flight clear timers (P0 §6 teardown hygiene). Idempotent."""
        tasks = list(self._clear_tasks)
        self._clear_tasks.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_active(self) -> None:
        """Stop any currently-active clip BEFORE publishing a new one (enforces one audible utterance)."""
        from . import audio_state
        pid = self.active_playback_id
        if pid is not None:
            cancel = getattr(self.link, "cancel_playback", None)
            if cancel is not None:
                try:
                    cancel(pid)
                except Exception:  # noqa: BLE001
                    pass
            self.active_playback_id = None
        audio_state.cancel(self._gen)   # clear the prior generation's gate/canceller (no-op if already gone)

    async def speak(self, text: str, *, check_say: bool = False, safety=None) -> dict:
        """Render + speak `text` on the robot speaker through the unified, SERIALIZED path (one active utterance
        at a time). `check_say` gates on the talk toggle / quiet window via `safety` (cortex `say` tool)."""
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

        async with self._play_lock:
            self.last_spoken = text
            await self._cancel_active()   # one audible utterance: cancel the previous before publishing
            try:
                pub = getattr(self.link, "publish_speech", None)
                if callable(pub):
                    wav = tts.render_wav(text)
                    if not wav:
                        return self._as_dict(await self.link.say_text(text))
                    dur = audio_state.wav_duration_s(wav)
                    cancellable = getattr(self.link, "cancel_playback", None) is not None
                    gen = audio_state.begin_playback(text, dur)   # gate ON + new generation
                    self._gen = gen
                    res = await pub(wav)
                    pid = res.get("playback_id") if isinstance(res, dict) else None
                    ok = (not isinstance(res, dict)) or (res.get("ok") is not False)
                    # Non-exception failure: ok=false, OR a cancellable native link gave no playback id (we
                    # could never cancel it). Clear the gate IMMEDIATELY — don't deafen the robot for `dur`.
                    if (not ok) or (cancellable and pid is None):
                        audio_state.clear(gen)
                        self.active_playback_id = None
                        why = "publish ok=false" if not ok else "cancellable link returned no playback_id"
                        return {"ok": False, "error": why}
                    self.active_playback_id = pid
                    if cancellable:
                        cancel = getattr(self.link, "cancel_playback")
                        audio_state.set_canceller(gen, lambda pid=pid, cancel=cancel: cancel(pid))
                    await self._emit({"type": "speech", "text": text, "b64": base64.b64encode(wav).decode(),
                                      "sr": 0, "ts": time.time()})
                    self._schedule_clear(gen, pid, dur)
                    return self._as_dict(res)
                if self.link.prefers_text_tts():
                    return self._as_dict(await self.link.say_text(text))
                g711 = tts.render_mulaw(text)
                if not g711:
                    return self._as_dict(await self.link.say_text(text))
                self._gen = audio_state.begin_playback(text, len(g711) / 8000.0)   # G.711 @ 8 kHz
                res = await self.link.say_audio(g711, codec="mulaw")
                if isinstance(res, dict) and res.get("ok") is False:
                    audio_state.clear(self._gen)
                    return {"ok": False, "error": "say_audio ok=false"}
                return self._as_dict(res)
            except Exception as e:  # noqa: BLE001
                audio_state.clear(self._gen)   # clear THIS clip's speaking state on failure
                self.active_playback_id = None
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _as_dict(res) -> dict:
        return res if isinstance(res, dict) else {"ok": bool(res)}
