"""The safety floor. Every robot-affecting action passes through here before reaching the robot link.

This is mechanical enforcement, not prompt trust: clamps, caps, rate limits, and gates that the AI cannot
bypass or change. See docs/SAFETY.md and .cursor/rules/30-safety.mdc.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from ..config import Settings


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    # normalized/clamped values the caller should actually use:
    ly: float = 0.0
    rx: float = 0.0
    duration: float = 0.0


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
        # Latched E-STOP is the hardest gate — it blocks EVERY source (manual/overseer included). Nothing
        # moves until /api/estop/reset clears the latch.
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
        generation so the caller can invalidate older in-flight drive commands. Idempotent."""
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

    def set_quiet(self, seconds: float) -> None:
        """'Shut up' — drop `say` for this many seconds (a temporary hush, distinct from the talk toggle)."""
        self._quiet_until = time.time() + max(0.0, seconds)

    def is_quiet(self) -> bool:
        return time.time() < self._quiet_until

    def check_say(self, s: Settings) -> Decision:
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
