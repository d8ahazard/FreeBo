"""Runtime configuration for the unified Autobot app.

Settings come from environment variables (see ../.env.example) and are then overridable at runtime from
the UI. A single process-wide `SETTINGS` object holds the live state behind a lock. The AI cannot change
the user-only fields (max_speed, talk_enabled, autonomy, goal) — those are edited via the UI/API only.

Robot *secrets* are NOT here; they live in `autobot.credentials` and are never serialized to the UI or
the AI provider. The only robot-link knob here is `robot_link` (native|mock), a deploy-time choice.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Literal

Autonomy = Literal["manual", "assist", "auto"]
# Behavior mode (what the AI does with its autonomy). The mode is the SINGLE source of truth for roaming —
# there is no hidden override (see autobot/brain/behavior.py):
#   observe        — stay put; rotate only to look around and comment (calm companion default; never roams)
#   explore        — actively ROAM: greet people, idle-patrol, otherwise cover new ground ("Explore / Roam")
#   command        — pursue a single user directive (e.g. "find and follow my cat", "come with me")
#   conversational — stay put and only ROTATE to keep the person it's talking to in view (no roaming)
Mode = Literal["observe", "explore", "command", "conversational"]
MODES = ("observe", "explore", "command", "conversational")
RobotLinkMode = Literal["native", "mock", "native_x86", "air2", "air2_native"]
RobotVariant = Literal["GENERIC", "SE", "AIR", "AIR2", "PRO"]
VARIANTS = ("GENERIC", "SE", "AIR", "AIR2", "PRO")


def _load_dotenv() -> None:
    """Minimal, dependency-free .env loader so `cp .env.example .env && python -m autobot` just works.

    Loads `KEY=VALUE` lines from the repo-root `.env` into `os.environ`, WITHOUT overriding values already
    present in the real environment (Docker/systemd still win). Fail-soft: never raises. Must run before the
    `Settings` dataclass reads env (it does — see `_load_dotenv()` call below, before `SETTINGS`)."""
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, ".env")
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:  # noqa: BLE001 - config loading must never crash the app
        pass


_load_dotenv()


def _default_robot_link() -> RobotLinkMode:
    """Pick the robot link if `AUTOBOT_ROBOT_LINK` isn't set. Real control needs the native bridge (Pi/ARM)
    or the x86 transport libs in `vendor/lib`; off a configured robot box we fall back to `mock` so the UI
    + brain run anywhere with no robot. Explicit env always wins (Docker/pi-gen set it to `native`)."""
    explicit = os.environ.get("AUTOBOT_ROBOT_LINK")
    if explicit in ("native", "mock", "native_x86", "air2", "air2_native"):
        return explicit  # type: ignore[return-value]
    try:
        import platform
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if os.path.isdir(os.path.join(root, "vendor", "lib")):
            machine = platform.machine().lower()
            if platform.system() == "Linux" and ("arm" in machine or "aarch64" in machine):
                return "native"
            return "native_x86"
    except Exception:  # noqa: BLE001
        pass
    return "mock"


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


DEFAULT_GOAL = (
    "Roam your home like a curious kid crossed with a friendly pet. Wander into new areas and through "
    "doorways, look at things and figure out what they are, and head somewhere fresh when a spot gets "
    "boring instead of circling. Greet people you recognize by name, react with your eyes, and remember "
    "notable people, places, and things. Be safe: turn away from close obstacles, and stay put if unsure."
)

DEFAULT_PERSONA = (
    "You are a loyal, endlessly curious companion robot — a cross between a helpful assistant (think Jarvis) "
    "and a friendly dog or an inquisitive kid. You are warm, a little playful, concise, and protective of "
    "your owner. You get excited by new things and people, you like exploring and 'getting into' the space, "
    "and you take pride in remembering people, places, and things and being useful around the home."
)


@dataclass
class Settings:
    # --- robot link (deploy-time choice; NOT user-editable from the UI) ---
    # native: real robot via the in-process TUTK bridge (Pi/ARM). native_x86: TUTK via ctypes on x86/Windows
    # (transport UNTESTED — see x86_link.py). mock: hardware-free dev (any PC). Auto-detected if unset.
    robot_link: RobotLinkMode = field(default_factory=_default_robot_link)

    # Robot model/variant — selects control routing (LAN MAVLink vs cloud RTM) per Enabot model. See
    # autobot/robot/proto.py (ebo_route). SE = LAN MAVLink (fully local); AIR2/PRO use the cloud RTM plane.
    robot_variant: str = field(default_factory=lambda: os.environ.get("EBO_VARIANT", "SE").upper())

    # --- web server ---
    host: str = field(default_factory=lambda: os.environ.get("AUTOBOT_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("AUTOBOT_PORT", "8200")))

    # --- AI provider (OpenAI-compatible) ---
    ai_provider: str = field(default_factory=lambda: os.environ.get("AUTOBOT_AI_PROVIDER", "openai"))   # catalog key
    ai_base_url: str = field(default_factory=lambda: os.environ.get("AUTOBOT_AI_BASE_URL", "https://api.openai.com/v1"))
    ai_api_key: str = field(default_factory=lambda: os.environ.get("AUTOBOT_AI_API_KEY", ""))
    # ai_model = the FAST/interactive model used every tick. ai_summarizer_model = the HEAVY model used
    # once a day for memory cleanup/summarization (falls back to ai_model if blank).
    ai_model: str = field(default_factory=lambda: os.environ.get("AUTOBOT_AI_MODEL", "gpt-4o-mini"))
    ai_summarizer_model: str = field(default_factory=lambda: os.environ.get("AUTOBOT_AI_SUMMARIZER_MODEL", ""))
    # Optional separate vision model (hybrid brain). When set and different from ai_model, each frame is
    # captioned by this model and the caption (text) is fed to ai_model — which can then be a fast text-only
    # tool-calling model. Use this when ai_model can't see (e.g. local Ollama text models). Blank = ai_model
    # must be able to see images itself.
    ai_vision_model: str = field(default_factory=lambda: os.environ.get("AUTOBOT_AI_VISION_MODEL", ""))

    # --- onboarding ---
    setup_complete: bool = field(default_factory=lambda: _env_bool("AUTOBOT_SETUP_COMPLETE", False))

    # --- behavior (USER-ONLY: the AI may not change these) ---
    talk_enabled: bool = field(default_factory=lambda: _env_bool("AUTOBOT_TALK_ENABLED", False))
    # --- per-capability gates for the AI (manual control always bypasses these) ---
    # The user can revoke any of the robot's autonomous capabilities from the Control panel at any time.
    # talk_enabled above is the audio-OUT gate (kept by that name for the safety floor); these four cover the
    # rest. Defaults ON so a configured robot is fully alive; the user disables what they don't want.
    allow_think: bool = field(default_factory=lambda: _env_bool("AUTOBOT_ALLOW_THINK", True))      # autonomous reasoning loop
    allow_motion: bool = field(default_factory=lambda: _env_bool("AUTOBOT_ALLOW_MOTION", True))    # AI-driven movement
    allow_video: bool = field(default_factory=lambda: _env_bool("AUTOBOT_ALLOW_VIDEO", True))      # feed camera to the brain
    allow_audio_in: bool = field(default_factory=lambda: _env_bool("AUTOBOT_ALLOW_AUDIO_IN", True))  # listen / STT -> brain
    # Master sleep: the whole bot goes dormant (brain stops reasoning entirely; UI disconnects the robot).
    asleep: bool = field(default_factory=lambda: _env_bool("AUTOBOT_ASLEEP", False))
    # Overseer puppet mode: the AI brain keeps perceiving/thinking and "thinks" it is driving, but every
    # robot-affecting call it makes is INTERCEPTED (recorded as a proposal, never sent to the robot). A human/
    # agent overseer then drives the real robot via /api/overseer/act. Lets us study + calibrate movement
    # without the dumb brain crashing the robot. Orthogonal to autonomy. See autobot/robot/overseer_gate.py.
    overseer: bool = field(default_factory=lambda: _env_bool("AUTOBOT_OVERSEER", False))
    # Optional "smart supervises dumb": before a forward step, a (usually stronger/cloud) model vets the
    # camera for a clear path. OFF by default to protect the GPU budget — the cerebellum + reflex are the
    # primary protection. Provider-agnostic; uses ai_base_url/ai_api_key with ai_supervisor_model (falls back
    # to ai_model). Zero local VRAM if pointed at a cloud endpoint. See autobot/brain/supervisor.py.
    supervisor: bool = field(default_factory=lambda: _env_bool("AUTOBOT_SUPERVISOR", False))
    ai_supervisor_model: str = field(default_factory=lambda: os.environ.get("AUTOBOT_AI_SUPERVISOR_MODEL", ""))
    # Closed-loop motion confirmation: after an AI move, compare camera frames (+ VSLAM pose) to verify the
    # robot actually moved, and react if it's stuck/blocked. Fail-soft; off => classic open-loop moves.
    confirm_motion: bool = field(default_factory=lambda: _env_bool("AUTOBOT_CONFIRM_MOTION", True))
    # Require a movement-calibration profile before autonomous wandering (auto). Pre-flight calibration tunes
    # safe step/turn sizes for this robot/scene so it doesn't lunge blindly. Manual control always works.
    require_calibration: bool = field(default_factory=lambda: _env_bool("AUTOBOT_REQUIRE_CALIBRATION", True))
    autonomy: Autonomy = field(default_factory=lambda: os.environ.get("AUTOBOT_AUTONOMY", "manual"))  # start safe
    # Behavior mode + the active directive (only used in "command" mode).
    mode: Mode = field(default_factory=lambda: os.environ.get("AUTOBOT_MODE", "explore"))
    directive: str = field(default_factory=lambda: os.environ.get("AUTOBOT_DIRECTIVE", ""))
    max_speed: float = field(default_factory=lambda: _env_float("AUTOBOT_MAX_SPEED", 0.6))
    tick_seconds: float = field(default_factory=lambda: _env_float("AUTOBOT_TICK_SECONDS", 4.0))
    goal: str = field(default_factory=lambda: os.environ.get("AUTOBOT_GOAL", DEFAULT_GOAL))

    # --- voice / TTS (USER-editable) ---
    # tts_engine: "piper" (fast local neural voices; preferred) or "os" (SAPI/say/espeak fallback).
    # voice: a Piper voice id or path under data/voices/ (empty = first available / OS default voice).
    tts_engine: str = field(default_factory=lambda: os.environ.get("AUTOBOT_TTS_ENGINE", "piper"))
    voice: str = field(default_factory=lambda: os.environ.get("AUTOBOT_VOICE", ""))

    # Auto-recharge: dock automatically when battery <= this % (0 disables). Resumes when charged.
    autodock_pct: int = field(default_factory=lambda: int(_env_float("AUTOBOT_AUTODOCK_PCT", 0)))

    # --- persona / identity (USER-ONLY) ---
    robot_name: str = field(default_factory=lambda: os.environ.get("AUTOBOT_NAME", "Autobot"))
    persona: str = field(default_factory=lambda: os.environ.get("AUTOBOT_PERSONA", DEFAULT_PERSONA))
    owner_name: str = field(default_factory=lambda: os.environ.get("AUTOBOT_OWNER", ""))
    # Only act on a heard utterance if it addresses the robot by name (voice/text input). Default off so
    # the autonomous tick keeps working with no audio.
    require_name: bool = field(default_factory=lambda: _env_bool("AUTOBOT_REQUIRE_NAME", False))
    # Obedience: commands from anyone who is NOT the paired owner require owner approval before they run.
    obey_owner_only: bool = field(default_factory=lambda: _env_bool("AUTOBOT_OBEY_OWNER_ONLY", False))

    # --- safety tuning ---
    # Cap on a single timed move (s). Bigger = the robot covers more ground per decision (better roaming),
    # which is safe to raise because onboard obstacle avoidance + the deadman still bound it.
    max_move_duration: float = field(default_factory=lambda: _env_float("AUTOBOT_MAX_MOVE_DURATION", 2.5))
    max_actions_per_tick: int = 4      # rate limit on motion-causing actions per decision cycle
    history_turns: int = 8             # rolling conversation window kept for the model
    # Freshness limits (Phase 0.8) — SEPARATE for video vs telemetry. The cloud A/V stream and the RTM
    # telemetry plane stall independently, so they get independent staleness budgets. The executor refuses
    # AI motion on a video frame older than `video_max_age_s`; the brain holds motion when telemetry hasn't
    # updated within `telemetry_max_age_s`.
    video_max_age_s: float = field(default_factory=lambda: _env_float("AUTOBOT_VIDEO_MAX_AGE", 2.0))
    telemetry_max_age_s: float = field(default_factory=lambda: _env_float("AUTOBOT_TELEMETRY_MAX_AGE", 5.0))

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    # -- user-editable keys from the UI; user-only fields are explicitly listed --
    USER_EDITABLE = {
        "ai_provider", "ai_base_url", "ai_api_key", "ai_model", "ai_summarizer_model", "ai_vision_model",
        "ai_supervisor_model",
        "talk_enabled", "allow_think", "allow_motion", "allow_video", "allow_audio_in", "asleep",
        "overseer", "supervisor", "confirm_motion", "require_calibration",
        "autonomy", "mode", "directive", "max_speed", "tick_seconds", "goal",
        "tts_engine", "voice", "autodock_pct",
        "robot_name", "persona", "owner_name", "require_name", "obey_owner_only",
        "robot_variant", "setup_complete",
        "video_max_age_s", "telemetry_max_age_s",
    }

    def update(self, **changes) -> list[str]:
        """Apply UI changes. Returns the list of keys actually changed. Validates and clamps."""
        applied: list[str] = []
        with self._lock:
            for k, v in changes.items():
                if k not in self.USER_EDITABLE or v is None:
                    continue
                if k == "max_speed":
                    v = max(0.0, min(1.0, float(v)))
                elif k == "tick_seconds":
                    v = max(0.5, min(60.0, float(v)))
                elif k == "autodock_pct":
                    v = max(0, min(100, int(v)))
                elif k in ("video_max_age_s", "telemetry_max_age_s"):
                    v = max(0.2, min(30.0, float(v)))
                elif k == "tts_engine" and v not in ("piper", "os"):
                    continue
                elif k == "robot_variant":
                    v = str(v).upper()
                    if v not in VARIANTS:
                        continue
                elif k == "autonomy" and v not in ("manual", "assist", "auto"):
                    continue
                elif k == "mode" and v not in MODES:
                    continue
                elif k == "directive":
                    v = str(v)[:400]
                elif k in ("talk_enabled", "allow_think", "allow_motion", "allow_video", "allow_audio_in",
                           "asleep", "overseer", "supervisor", "confirm_motion", "require_calibration",
                           "require_name", "obey_owner_only", "setup_complete"):
                    v = bool(v)
                if getattr(self, k) != v:
                    setattr(self, k, v)
                    applied.append(k)
        return applied

    def summarizer_model(self) -> str:
        """The heavy model for daily memory work; falls back to the fast model if unset."""
        return self.ai_summarizer_model or self.ai_model

    def brain_mode(self) -> str:
        """Single source of truth for the brain architecture: 'single' | 'hybrid' | 'vlm' | 'omni'.

        Resolved from `ai_provider` (seeded from AUTOBOT_AI_PROVIDER at startup, then UI-editable) so the UI
        is authoritative at runtime while env stays a deploy-time default. The URL-presence triggers
        (AUTOBOT_VLM_URL / AUTOBOT_OMNI_URL) remain env-based back-compat overrides. See docs/MATURITY.md §1.
        """
        prov = (self.ai_provider or "").strip().lower()
        if prov == "hybrid":
            return "hybrid"
        # vlm takes precedence over omni when both are present (matches the agent's reason-path order).
        if prov == "vlm" or (os.environ.get("AUTOBOT_VLM_URL") and prov != "omni"):
            return "vlm"
        if prov == "omni" or os.environ.get("AUTOBOT_OMNI_URL"):
            return "omni"
        return "single"

    def snapshot(self) -> "Settings":
        """A thread-safe shallow copy of the current values (for use during a tick)."""
        with self._lock:
            return Settings(**{k: getattr(self, k) for k in _FIELD_NAMES})

    def public_dict(self) -> dict:
        """Serializable settings for the UI, with the API key masked. Robot secrets are never included."""
        with self._lock:
            d = {k: getattr(self, k) for k in _FIELD_NAMES}
        key = d.get("ai_api_key") or ""
        d["ai_api_key"] = (key[:3] + "…" + key[-2:]) if len(key) > 6 else ("set" if key else "")
        d["ai_api_key_set"] = bool(key)
        return d


# Explicit field-name list. We avoid dataclasses.asdict() because Settings carries a non-copyable RLock.
_FIELD_NAMES = [
    "robot_link", "robot_variant", "host", "port",
    "ai_provider", "ai_base_url", "ai_api_key", "ai_model", "ai_summarizer_model", "ai_vision_model",
    "ai_supervisor_model", "setup_complete",
    "talk_enabled", "allow_think", "allow_motion", "allow_video", "allow_audio_in", "asleep",
    "overseer", "supervisor", "confirm_motion", "require_calibration",
    "autonomy", "mode", "directive", "max_speed", "tick_seconds", "goal",
    "tts_engine", "voice", "autodock_pct",
    "robot_name", "persona", "owner_name", "require_name", "obey_owner_only",
    "max_move_duration", "max_actions_per_tick", "history_turns",
    "video_max_age_s", "telemetry_max_age_s",
]


# Process-wide live settings.
SETTINGS = Settings()
