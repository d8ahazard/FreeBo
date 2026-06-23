"""Skill base types + the shared context handed to every skill."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ...config import Settings
from ...robot.link import RobotLink
from ..identity import Identity
from ..memory import Memory
from ..safety import SafetyFloor

# authority levels for a tool: "anyone" (always allowed) or "owner" (gated by the obedience policy).
Authority = str


@dataclass
class ToolDef:
    schema: dict
    handler: Callable[[dict], Awaitable[dict]]
    authority: Authority = "anyone"


@dataclass
class SkillContext:
    """Everything a skill needs. Persistent for the process (settings is the LIVE object)."""
    link: RobotLink
    settings: Settings
    safety: SafetyFloor
    memory: Memory
    identity: Identity
    emit: Callable[[dict], Awaitable[None]]
    # scratch flags shared within a tick (e.g. the `look` request); the agent reads/clears these.
    flags: dict[str, Any] = field(default_factory=dict)
    # latest thing the robot "heard" (set by the voice skill): {"text", "ts"}.
    heard: dict[str, Any] = field(default_factory=dict)
    # optional hook the agent sets: called with (text, speaker) when speech is heard, so the event-driven
    # reasoner can react immediately (preempting idle wandering). Default no-op.
    on_speech: Callable[[str, str], None] | None = None
    # optional hook the agent sets: nudge the reasoner to run a cycle now (e.g. when a known face appears so
    # the robot greets promptly). Default no-op.
    wake: Callable[[], None] | None = None
    # eye animations available this tick (for building the set_eyes enum).
    eye_animations: list[str] = field(default_factory=list)
    # optional VSLAM pose source (set by the agent/server): returns a `/api/slam/map`-shaped dict
    # ({"enabled", "pose": {"x","y","yaw_deg"}, ...}) so spatial skills can tag/navigate by rough pose.
    pose_provider: Callable[[], dict] | None = None
    # the brain's persistent task/reminder store (set by the agent); the tasks skill edits it, the brain's
    # scheduler loop fires due tasks. None => the tasks skill is inactive.
    tasks: Any = None
    # calibrated movement profile (set by the agent): controlled step/turn sizes so drive bursts stay small.
    motion_profile: Any = None
    # the behavior controller (set by the agent): voice/paraphrase commands can steer it (explore/stay/...).
    behavior: Any = None


class Skill:
    """Base skill. Override what you need; defaults are no-ops so skills stay small."""

    name: str = "skill"

    def available(self, ctx: SkillContext) -> tuple[bool, str]:
        """(active, reason). Inactive skills contribute no tools/prompt. Default: active."""
        return True, ""

    def tools(self, ctx: SkillContext) -> list[ToolDef]:
        return []

    def system_prompt_fragment(self, ctx: SkillContext) -> str:
        return ""

    async def on_observe(self, ctx: SkillContext, observation) -> None:
        """Called once per perception tick with the fresh Observation (e.g. run recognition)."""
        return None

    def background_workers(self, ctx: SkillContext) -> list[Callable[[], None]]:
        """Return callables to run on daemon threads once at startup (e.g. an audio listener)."""
        return []


def fn_schema(name: str, desc: str, params: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}
