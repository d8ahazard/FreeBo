"""Voice input skill — gives the robot ears.

Taps the robot's inbound G.711 mic audio (via `RobotLink.set_audio_sink`), buffers it, and transcribes
short chunks with an OPTIONAL speech-to-text engine (`faster_whisper` preferred, else `whisper`). The latest
transcript is written to the shared context (`ctx.heard`); the agent loop reads it, applies the
"only respond to my name" addressing gate, and feeds addressed speech into the model — which can then reply
via the `say` tool. Entirely optional: with no STT engine (or no audio, e.g. mock), the skill is inactive
and the rest of the robot is unaffected.
"""
from __future__ import annotations

import os
import threading
import time
import wave
from pathlib import Path

from .base import Skill, SkillContext

CHUNK_SECONDS = 2.5          # transcribe in ~2.5s windows
SAMPLE_RATE = 8000           # robot mic is G.711 8 kHz mono
STT_RATE = 16000             # whisper wants 16 kHz
MODEL_NAME = os.environ.get("AUTOBOT_STT_MODEL", "base")


def _detect_engine():
    try:
        from faster_whisper import WhisperModel  # type: ignore
        return "faster_whisper"
    except Exception:  # noqa: BLE001
        pass
    try:
        import whisper  # type: ignore  # noqa: F401
        return "whisper"
    except Exception:  # noqa: BLE001
        return None


class VoiceSkill(Skill):
    name = "voice"

    def __init__(self):
        self.engine = _detect_engine()
        self._buf = bytearray()        # accumulated G.711 mu-law bytes
        self._lock = threading.Lock()
        self._model = None
        self._registered = False

    def available(self, ctx: SkillContext) -> tuple[bool, str]:
        if not self.engine:
            return False, "no STT engine (pip install faster-whisper)"
        # Register the audio sink once; if the link has no audio (mock), this is a harmless no-op.
        if not self._registered:
            try:
                ctx.link.set_audio_sink(self._on_audio)
                self._registered = True
            except Exception:  # noqa: BLE001
                return False, "link has no audio"
        return True, ""

    def _on_audio(self, mulaw: bytes):
        with self._lock:
            self._buf += mulaw
            # cap the buffer so we never grow unbounded if transcription stalls
            if len(self._buf) > SAMPLE_RATE * 10:
                del self._buf[:len(self._buf) - SAMPLE_RATE * 10]

    def background_workers(self, ctx: SkillContext):
        return [lambda: self._run(ctx)]

    def _run(self, ctx: SkillContext):
        need = int(SAMPLE_RATE * CHUNK_SECONDS)
        while True:
            time.sleep(0.4)
            with self._lock:
                if len(self._buf) < need:
                    continue
                chunk = bytes(self._buf[:need])
                del self._buf[:need]
            text = self._transcribe(chunk)
            if text:
                if ctx.on_speech:
                    try:
                        ctx.on_speech(text, "voice")
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    ctx.heard.clear()
                    ctx.heard.update({"text": text, "ts": time.time(), "speaker": "voice"})
                print(f"[voice] heard: {text!r}", flush=True)

    def _transcribe(self, mulaw: bytes) -> str:
        import audioop
        import tempfile
        try:
            pcm = audioop.ulaw2lin(mulaw, 2)
            pcm, _ = audioop.ratecv(pcm, 2, 1, SAMPLE_RATE, STT_RATE, None)
        except Exception:  # noqa: BLE001
            return ""
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(STT_RATE); w.writeframes(pcm)
            return self._run_model(path)
        except Exception as e:  # noqa: BLE001
            print(f"[voice] transcribe failed: {e}", flush=True)
            return ""
        finally:
            try: Path(path).unlink()
            except OSError: pass

    def _run_model(self, wav_path: str) -> str:
        if self.engine == "faster_whisper":
            if self._model is None:
                from faster_whisper import WhisperModel  # type: ignore
                self._model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
            segments, _ = self._model.transcribe(wav_path, vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()
        if self.engine == "whisper":
            if self._model is None:
                import whisper  # type: ignore
                self._model = whisper.load_model(MODEL_NAME)
            return str(self._model.transcribe(wav_path).get("text", "")).strip()
        return ""
