"""The safety floor. Every robot-affecting action passes through here before reaching the robot link.

This is mechanical enforcement, not prompt trust: clamps, caps, rate limits, and gates that the AI cannot
bypass or change. See docs/SAFETY.md and .cursor/rules/30-safety.mdc.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from ..config import Settings

# The five autonomous faculties the kernel is the single authority for (P0-R4.1).
CAP_THINK = "think"
CAP_MOTION = "motion"
CAP_SPEAK = "speak"
CAP_LISTEN = "listen"
CAP_SEE = "ai_vision"
CAPABILITIES = (CAP_THINK, CAP_MOTION, CAP_SPEAK, CAP_LISTEN, CAP_SEE)


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    # normalized/clamped values the caller should actually use:
    ly: float = 0.0
    rx: float = 0.0
    duration: float = 0.0


@dataclass
class FacultyDecision:
    """The authoritative answer for one faculty (P0-R4.1). Callers must act on `effective_enabled`/`allowed`
    and the `reason`, NOT re-interpret allow_*/talk_enabled/asleep/latch themselves."""
    allowed: bool
    capability: str
    reason: str = ""
    master_inhibited: bool = False
    requested_enabled: bool = False
    effective_enabled: bool = False
    generation: int = 0
    ts: float = 0.0


class SafetyFloor:
    """Holds per-tick rate-limit state. One instance lives for the whole process."""

    def __init__(self):
        self._tick_start = time.monotonic()
        self._actions_this_tick = 0
        self._scope = "roam"   # movement scope for this cycle: roam | adjust (rotate only) | hold (no motion)
        self._quiet_until = 0.0  # 'shut up' window: drop `say` until this time
        # Latched emergency stop (P0-R3.2): a STATE, not a one-shot. While latched, EVERY motion source is
        # rejected (ai/recovery/manual/overseer) until an explicit reset. Bumping the control generation on
        # latch lets the sidecar drop any in-flight/stale drive frames from before the stop.
        self._estop_latched = False
        self._control_generation = 0
        # Master autonomous-faculty inhibit (P0-R4.2): set by STOP, cleared by RESUME. While set, EVERY
        # autonomous faculty (think/motion/speak/listen/see) is denied. Distinct from the motion latch (which
        # STOP also sets): RESUME clears this only after the link/sidecar latch+generation are reconciled.
        self._master_inhibited = False

    def begin_tick(self):
        self._tick_start = time.monotonic()
        self._actions_this_tick = 0

    def set_scope(self, scope: str) -> None:
        """Set the allowed movement scope for AI drives this cycle (set by the behavior controller):
        'roam' = drive freely (still clamped), 'adjust' = rotate in place only (no translation),
        'hold' = no AI motion at all. Manual control bypasses this."""
        self._scope = scope if scope in ("roam", "adjust", "hold") else "roam"

    # --- gates ---
    def autonomy_allows_motion(self, s: Settings) -> bool:
        # In 'manual' the AI may perceive/think but not move. assist/auto may move (clamped).
        return s.autonomy in ("assist", "auto")

    def check_drive(self, s: Settings, ly: float, rx: float, duration: float, *,
                    source: str = "ai") -> Decision:
        """Clamp a drive/move request. AI sources ('ai' and 'recovery' — an executor-proposed recovery move)
        are rate-limited + autonomy-gated + scope-gated; 'manual' (UI) and 'overseer' (the human/agent
        puppeting the robot in overseer mode) are only speed/duration-clamped — the human is in control, so
        they bypass the AI motion/autonomy/rate gates but still cannot exceed the speed/duration caps (the
        speed clamp is non-negotiable; see .cursor/rules/30-safety.mdc)."""
        # Master STOP inhibit + latched E-STOP are the hardest gates — they block EVERY source
        # (manual/overseer included). Nothing moves until RESUME reconciles + /api/estop/reset clears it.
        if self._master_inhibited:
            return Decision(False, "master_inhibited (STOP)")
        if self._estop_latched:
            return Decision(False, "estop_latched")
        ai = source in ("ai", "recovery")
        if ai and not getattr(s, "allow_motion", True):
            return Decision(False, "motion disabled by the user (Control toggle)")
        if ai and not self.autonomy_allows_motion(s):
            return Decision(False, f"autonomy is '{s.autonomy}': AI motion blocked")
        if ai:
            if self._actions_this_tick >= s.max_actions_per_tick:
                return Decision(False, f"rate limit: >{s.max_actions_per_tick} actions/tick")
            self._actions_this_tick += 1
        # Movement scope (set by the behavior controller; hard guarantee independent of the prompt):
        #   hold   -> no AI motion; adjust -> rotate in place only (zero translation); roam -> normal.
        # Conversational mode also forces rotate-only as a belt-and-braces floor.
        if ai:
            if self._scope == "hold":
                return Decision(False, "holding position (behavior: not roaming right now)")
            if self._scope == "adjust" or getattr(s, "mode", "explore") == "conversational":
                ly = 0.0
        # clamp magnitude to max_speed
        ly, rx = _clamp_vector(ly, rx, s.max_speed)
        duration = max(0.0, min(float(duration or 0.0), s.max_move_duration))
        return Decision(True, "", ly=ly, rx=rx, duration=duration)

    # --- latched emergency stop ---
    def estop_latch(self) -> int:
        """Latch the emergency stop (blocks all motion) and bump the control generation. Returns the new
        generation so the caller can invalidate older in-flight drive commands."""
        self._estop_latched = True
        self._control_generation += 1
        return self._control_generation

    def estop_reset(self) -> None:
        """Clear the latch so motion is PERMITTED again (does NOT, by itself, enable autonomous movement)."""
        self._estop_latched = False

    def is_latched(self) -> bool:
        return self._estop_latched

    def control_generation(self) -> int:
        return self._control_generation

    # --- master autonomous-faculty inhibit (STOP/RESUME) ---
    def master_inhibit(self) -> int:
        """STOP: inhibit ALL autonomous faculties, latch motion, and bump the control generation so the
        sidecar drops any stale in-flight drives. Returns the new generation. Idempotent: the generation
        bumps once per STOP event (on the transition into inhibited), so calling it twice in one STOP is safe."""
        if not self._master_inhibited:
            self._control_generation += 1
        self._master_inhibited = True
        self._estop_latched = True
        return self._control_generation

    def master_release(self) -> None:
        """RESUME: clear the master faculty inhibit. The caller MUST have reconciled the link/sidecar
        latch+generation first; the motion latch is cleared separately via estop_reset()."""
        self._master_inhibited = False

    def is_master_inhibited(self) -> bool:
        return self._master_inhibited

    # --- central faculty authority (P0-R4.1) ---
    def _faculty(self, capability: str, requested: bool, s: Settings,
                 extra_reason: str = "") -> FacultyDecision:
        """Common faculty decision: master inhibit > asleep > requested toggle > faculty-specific extra."""
        if self._master_inhibited:
            eff, reason = False, "master STOP"
        elif getattr(s, "asleep", False):
            eff, reason = False, "asleep"
        elif not requested:
            eff, reason = False, f"{capability} ability off"
        elif extra_reason:
            eff, reason = False, extra_reason
        else:
            eff, reason = True, ""
        return FacultyDecision(allowed=eff, capability=capability, reason=reason,
                               master_inhibited=self._master_inhibited, requested_enabled=requested,
                               effective_enabled=eff, generation=self._control_generation, ts=time.time())

    def check_think(self, s: Settings) -> FacultyDecision:
        return self._faculty(CAP_THINK, bool(getattr(s, "allow_think", True)), s)

    def check_listen(self, s: Settings) -> FacultyDecision:
        return self._faculty(CAP_LISTEN, bool(getattr(s, "allow_audio_in", True)), s)

    def check_see(self, s: Settings) -> FacultyDecision:
        return self._faculty(CAP_SEE, bool(getattr(s, "allow_video", True)), s)

    def check_speak(self, s: Settings) -> FacultyDecision:
        """Faculty view of speech (talk_enabled + not quieted). The clamped path stays check_say()."""
        extra = "quieted" if (getattr(s, "talk_enabled", False) and self.is_quiet()) else ""
        return self._faculty(CAP_SPEAK, bool(getattr(s, "talk_enabled", False)), s, extra)

    def check_motion(self, s: Settings) -> FacultyDecision:
        """Faculty view of AI motion (lightweight — the rich readiness/blockers live in the agent). effective
        means: requested + not master/asleep/latched + autonomy permits AI motion + scope not hold."""
        requested = bool(getattr(s, "allow_motion", True))
        extra = ""
        if not self._master_inhibited and not getattr(s, "asleep", False) and requested:
            if self._estop_latched:
                extra = "estop_latched"
            elif not self.autonomy_allows_motion(s):
                extra = f"autonomy '{s.autonomy}'"
            elif self._scope == "hold":
                extra = "behavior hold"
        return self._faculty(CAP_MOTION, requested, s, extra)

    def capability_snapshot(self, s: Settings, motion_reason: str = "") -> dict:
        """The authoritative capability-state snapshot (P0-R4.6). `motion_reason` lets the agent supply the
        richer motion block reason (telemetry/video/calibration/etc.) computed with full context."""
        caps: dict[str, dict] = {}
        for fd in (self.check_think(s), self.check_motion(s), self.check_speak(s),
                   self.check_listen(s), self.check_see(s)):
            caps[fd.capability] = {"requested": fd.requested_enabled, "effective": fd.effective_enabled,
                                   "reason": fd.reason}
        if motion_reason and not caps[CAP_MOTION]["effective"]:
            caps[CAP_MOTION]["reason"] = caps[CAP_MOTION]["reason"] or motion_reason
        elif motion_reason and caps[CAP_MOTION]["effective"]:
            # The faculty view says OK but the agent found a richer blocker (stale telem/video, etc.).
            caps[CAP_MOTION]["effective"] = False
            caps[CAP_MOTION]["reason"] = motion_reason
        return {"master_inhibited": self._master_inhibited, "generation": self._control_generation,
                "capabilities": caps, "ts": time.time()}

    def set_quiet(self, seconds: float) -> None:
        """'Shut up' — drop `say` for this many seconds (a temporary hush, distinct from the talk toggle)."""
        self._quiet_until = time.time() + max(0.0, seconds)

    def is_quiet(self) -> bool:
        return time.time() < self._quiet_until

    def check_say(self, s: Settings) -> Decision:
        if self._master_inhibited:
            return Decision(False, "master_inhibited (STOP)")
        if getattr(s, "asleep", False):
            return Decision(False, "asleep")
        if not s.talk_enabled:
            return Decision(False, "talk disabled (UI toggle off)")
        if self.is_quiet():
            return Decision(False, "quieted (told to be quiet)")
        return Decision(True)

    def count_action(self):
        self._actions_this_tick += 1


def _clamp_vector(ly: float, rx: float, max_speed: float) -> tuple[float, float]:
    ly = float(ly or 0.0)
    rx = float(rx or 0.0)
    mag = math.hypot(ly, rx)
    if mag > max_speed and mag > 0:
        scale = max_speed / mag
        ly *= scale
        rx *= scale
    # also hard-clamp each axis to [-1, 1] just in case
    ly = max(-1.0, min(1.0, ly))
    rx = max(-1.0, min(1.0, rx))
    return round(ly, 3), round(rx, 3)
