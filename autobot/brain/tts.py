"""PC-side text-to-speech -> G.711 µ-law, for talkback.

The robot's listen codec is G.711 (8 kHz mono µ-law). We synthesize speech on the host and convert it to raw
µ-law bytes here; the link forwards them to the robot's speaker. Conversion uses ffmpeg (already a dep for
video).

Two engines, picked from config.tts_engine (fail-soft, auto-fallback):
  - **piper** (preferred): fast local neural voices. Any Piper `.onnx` voice in data/voices/ — Jarvis-like,
    Hulk-like, or any custom voice — selectable per `config.voice`. Needs the `piper` binary on PATH (or the
    `piper-tts` pip package). Quality ceiling is the robot's 8 kHz speaker, but the voice identity carries.
  - **os** (fallback): Windows SAPI / macOS `say` / Linux espeak-ng. Always-available zero-setup default.

If neither yields audio, render_mulaw() returns None and the caller reports talkback-TTS unavailable.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VOICES_DIR = Path(os.environ.get("AUTOBOT_VOICES_DIR", str(REPO_ROOT / "data" / "voices")))


def _settings():
    try:
        from ..config import SETTINGS
        return SETTINGS.snapshot()
    except Exception:  # noqa: BLE001
        return None


def piper_bin() -> str | None:
    """Locate a WORKING piper executable. We prefer the bundled official binary (data/tools/piper/) — the
    `piper-tts` pip package crashes natively on some Windows setups, so the self-contained binary is the
    reliable path. Override with AUTOBOT_PIPER_BIN."""
    env = os.environ.get("AUTOBOT_PIPER_BIN")
    if env and Path(env).is_file():
        return env
    name = "piper.exe" if platform.system() == "Windows" else "piper"
    bundled = REPO_ROOT / "data" / "tools" / "piper" / name
    if bundled.is_file():
        return str(bundled)
    return shutil.which("piper")


def list_voices() -> list[str]:
    """Voice ids available for Piper = the stem of each .onnx model in data/voices/."""
    try:
        return sorted(p.stem for p in VOICES_DIR.glob("*.onnx"))
    except Exception:  # noqa: BLE001
        return []


def _resolve_voice_model(voice: str) -> str | None:
    """Map a configured voice (id or path) to an existing .onnx model file."""
    if not voice:
        voices = list_voices()
        if not voices:
            return None
        voice = voices[0]
    p = Path(voice)
    if p.suffix == ".onnx" and p.is_file():
        return str(p)
    cand = VOICES_DIR / (voice if voice.endswith(".onnx") else f"{voice}.onnx")
    return str(cand) if cand.is_file() else None


def available(engine: str | None = None, voice: str | None = None) -> tuple[bool, str]:
    """Returns (ok, backend_name) for the currently-selected engine."""
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg missing (needed to convert to G.711)"
    s = _settings()
    engine = engine or (s.tts_engine if s else "piper")
    voice = voice if voice is not None else (s.voice if s else "")
    if engine == "piper":
        model = _resolve_voice_model(voice)
        if piper_bin() and model:
            return True, f"piper:{Path(model).stem}"
        # piper requested but unusable -> report why, then fall through to OS availability
        if not piper_bin():
            note = "piper not installed"
        else:
            note = "no .onnx voice in data/voices (run scripts/get_voice.py)"
        ok, os_be = _os_available()
        return (ok, f"os:{os_be} (piper unavailable: {note})") if ok else (False, f"piper unavailable: {note}")
    ok, os_be = _os_available()
    return ok, f"os:{os_be}"


def _os_available() -> tuple[bool, str]:
    sysname = platform.system()
    if sysname == "Windows":
        return True, "powershell-sapi"
    if sysname == "Darwin" and shutil.which("say"):
        return True, "macos-say"
    if shutil.which("espeak-ng"):
        return True, "espeak-ng"
    if shutil.which("espeak"):
        return True, "espeak"
    return False, "no OS TTS engine (install espeak-ng / use macOS say / Windows SAPI)"


def render_mulaw(text: str, voice: str | None = None, engine: str | None = None) -> bytes | None:
    """Synthesize `text` and return raw G.711 µ-law @ 8 kHz mono, or None on failure."""
    if not text.strip() or not shutil.which("ffmpeg"):
        return None
    s = _settings()
    engine = engine or (s.tts_engine if s else "piper")
    voice = voice if voice is not None else (s.voice if s else "")

    src = None
    if engine == "piper":
        src = _synthesize_piper(text, voice)
    if not src:
        src = _synthesize_os(text)
    if not src:
        return None
    try:
        return _to_mulaw(src)
    finally:
        try:
            os.remove(src)
        except OSError:
            pass


def _synthesize_piper(text: str, voice: str) -> str | None:
    """Run Piper: text on stdin -> WAV file. Returns the wav path, or None to fall back to OS TTS."""
    binp = piper_bin()
    model = _resolve_voice_model(voice)
    if not binp or not model:
        return None
    try:
        fd, wav = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        subprocess.run([binp, "--model", model, "--output_file", wav],
                       input=text.encode("utf-8"), check=True, capture_output=True, timeout=60)
        return wav if os.path.getsize(wav) > 0 else None
    except Exception:  # noqa: BLE001 - fail soft -> OS fallback
        return None


def _synthesize_os(text: str) -> str | None:
    sysname = platform.system()
    try:
        if sysname == "Windows":
            fd, wav = tempfile.mkstemp(suffix=".wav"); os.close(fd)
            tf = wav + ".txt"
            with open(tf, "w", encoding="utf-8") as f:
                f.write(text)
            ps = (
                "$ErrorActionPreference='Stop';"
                "Add-Type -AssemblyName System.Speech;"
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                f"$s.SetOutputToWaveFile('{wav}');"
                f"$t=Get-Content -Raw -LiteralPath '{tf}';"
                "$s.Speak($t);$s.Dispose();"
            )
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           check=True, capture_output=True, timeout=30)
            try: os.remove(tf)
            except OSError: pass
            return wav if os.path.getsize(wav) > 0 else None
        if sysname == "Darwin":
            fd, aiff = tempfile.mkstemp(suffix=".aiff"); os.close(fd)
            subprocess.run(["say", "-o", aiff, text], check=True, capture_output=True, timeout=30)
            return aiff if os.path.getsize(aiff) > 0 else None
        engine = "espeak-ng" if shutil.which("espeak-ng") else "espeak"
        if not shutil.which(engine):
            return None
        fd, wav = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        subprocess.run([engine, "-w", wav, "-s", "150", text], check=True, capture_output=True, timeout=30)
        return wav if os.path.getsize(wav) > 0 else None
    except Exception:  # noqa: BLE001 - fail soft
        return None


def render_wav(text: str, voice: str | None = None, engine: str | None = None) -> bytes | None:
    """Synthesize `text` and return WAV bytes (16 kHz mono) — used for the Air 2 cloud path, where the
    browser publishes this audio into the Agora call so the robot's own speaker plays it."""
    if not text.strip():
        return None
    s = _settings()
    engine = engine or (s.tts_engine if s else "piper")
    voice = voice if voice is not None else (s.voice if s else "")
    src = _synthesize_piper(text, voice) if engine == "piper" else None
    if not src:
        src = _synthesize_os(text)
    if not src:
        return None
    try:
        if shutil.which("ffmpeg"):
            wav = _to_wav(src)
            if wav:
                return wav
        # fallback: if the synth already produced a .wav, return it raw
        if src.endswith(".wav"):
            with open(src, "rb") as f:
                return f.read()
        return None
    finally:
        try:
            os.remove(src)
        except OSError:
            pass


def _to_wav(src_path: str) -> bytes | None:
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", src_path,
             "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
            check=True, capture_output=True, timeout=30,
        )
        return p.stdout or None
    except Exception:  # noqa: BLE001
        return None


def _to_mulaw(src_path: str) -> bytes | None:
    """ffmpeg: any audio file -> raw G.711 µ-law, 8 kHz mono."""
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", src_path,
             "-ar", "8000", "-ac", "1", "-f", "mulaw", "pipe:1"],
            check=True, capture_output=True, timeout=30,
        )
        return p.stdout or None
    except Exception:  # noqa: BLE001
        return None
