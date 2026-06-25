"""The safety floor. Every robot-affecting action passes through here before reaching the robot link.

This is mechanical enforcement, not prompt trust: clamps, caps, rate limits, and gates that the AI cannot
bypass or change. See docs/SAFETY.md and .cursor/rules/30-safety.mdc.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

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
class ResetToken:
    """A compare-and-swap snapshot captured when a RESET begins (P0-R4 atomicity). The reset may commit only
    if the arbiter's (epoch, generation) are STILL these values when the sidecar response arrives — so a newer
    STOP (which advanced the epoch) makes the old reset fail, even if the system was already inhibited."""
    reset_attempt_id: int
    epoch: int
    generation: int


class ControlArbiter:
    """The ONE process-side control-transition authority (P0-R4 atomicity amendment). Every STOP / RESET /
    reconnect / drive-admission consults this single RLock-protected object so latch + generation + epoch
    transitions are atomic and monotonic. STOP always advances the transition epoch AND generation (idempotent
    STATE is fine; idempotent transition IDENTITY is not). Observed-sidecar state lives in the link layer
    (RtmNode); the arbiter owns the DESIRED state + the instance check is done link-side."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.transition_epoch = 0
        self.desired_generation = 0
        self.desired_latched = False
        self.master_inhibited = False
        self.estop_in_flight = False
        self._reset_attempt = 0

    def begin_master_stop(self) -> dict:
        """STOP: assert local inhibit + motion latch, ALWAYS advance epoch + generation (even if already
        inhibited), mark an E-STOP dispatch in flight. Returns the exact {generation, epoch} the caller must
        stamp on the transport command. The single synchronous entry for every STOP source."""
        with self._lock:
            self.transition_epoch += 1
            self.desired_generation += 1
            self.desired_latched = True
            self.master_inhibited = True
            self.estop_in_flight = True
            return {"generation": self.desired_generation, "epoch": self.transition_epoch}

    def latch_motion(self) -> dict:
        """Motion-only latch (non-master reflex/barge-in). Still monotonic in epoch + generation."""
        with self._lock:
            self.transition_epoch += 1
            self.desired_generation += 1
            self.desired_latched = True
            return {"generation": self.desired_generation, "epoch": self.transition_epoch}

    def end_estop_dispatch(self) -> None:
        with self._lock:
            self.estop_in_flight = False

    def begin_reset(self) -> ResetToken:
        with self._lock:
            self._reset_attempt += 1
            return ResetToken(self._reset_attempt, self.transition_epoch, self.desired_generation)

    def commit_reset(self, token: ResetToken) -> bool:
        """CAS: clear the desired latch/inhibit ONLY if no transition happened since the reset began and no
        E-STOP dispatch is in flight. A newer STOP (advanced epoch) makes this fail."""
        with self._lock:
            if self.estop_in_flight:
                return False
            if token.epoch != self.transition_epoch or token.generation != self.desired_generation:
                return False
            self.desired_latched = False
            self.master_inhibited = False
            return True

    def force_clear(self) -> None:
        """Unconditional unlatch/release (simple links / mock with no sidecar to reconcile)."""
        with self._lock:
            self.desired_latched = False
            self.master_inhibited = False

    def snapshot(self) -> dict:
        with self._lock:
            return {"transition_epoch": self.transition_epoch, "desired_generation": self.desired_generation,
                    "desired_latched": self.desired_latched, "master_inhibited": self.master_inhibited,
                    "estop_in_flight": self.estop_in_flight}


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
        # The single control-transition authority (P0-R4 atomicity). All latch/inhibit/generation/epoch state
        # lives here so STOP/RESET/drive-admission transitions are atomic + monotonic.
        self.arb = ControlArbiter()

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
        # (manual/overseer included). Nothing moves until RESUME reconciles + clears it.
        if self.arb.master_inhibited:
            return Decision(False, "master_inhibited (STOP)")
        if self.arb.desired_latched:
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
        """Motion-only latch (reflex/barge-in). Bumps generation + epoch via the arbiter. Returns generation."""
        return self.arb.latch_motion()["generation"]

    # --- the single STOP/RESET transition entries (P0-R4 atomicity) ---
    def begin_master_stop(self) -> dict:
        """The ONE synchronous master-STOP transition: assert inhibit+latch, advance epoch+generation ALWAYS,
        mark dispatch in flight. Returns {generation, epoch} to stamp on the transport command."""
        return self.arb.begin_master_stop()

    def end_estop_dispatch(self) -> None:
        self.arb.end_estop_dispatch()

    def begin_reset(self) -> ResetToken:
        return self.arb.begin_reset()

    def commit_reset(self, token: ResetToken) -> bool:
        return self.arb.commit_reset(token)

    def transition_epoch(self) -> int:
        return self.arb.transition_epoch

    def estop_reset(self) -> None:
        """Unconditional unlatch/release (simple links / mock). The reconciled CAS path is begin_reset/
        commit_reset; use that with a sidecar."""
        self.arb.force_clear()

    def is_latched(self) -> bool:
        return self.arb.desired_latched

    def control_generation(self) -> int:
        return self.arb.desired_generation

    # --- master autonomous-faculty inhibit (STOP/RESUME) ---
    def master_inhibit(self) -> int:
        """Back-compat alias for begin_master_stop(); returns the new generation. Prefer begin_master_stop()
        which returns the epoch too. STOP always advances epoch + generation (one transition identity)."""
        return self.arb.begin_master_stop()["generation"]

    def master_release(self) -> None:
        """RESUME: clear the master faculty inhibit. Callers should prefer commit_reset() (CAS); this is the
        unconditional release used after a validated reconciliation."""
        self.arb.force_clear()

    def is_master_inhibited(self) -> bool:
        return self.arb.master_inhibited

    # --- central faculty authority (P0-R4.1) ---
    def _faculty(self, capability: str, requested: bool, s: Settings,
                 extra_reason: str = "") -> FacultyDecision:
        """Common faculty decision: master inhibit > asleep > requested toggle > faculty-specific extra."""
        if self.arb.master_inhibited:
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
                               master_inhibited=self.arb.master_inhibited, requested_enabled=requested,
                               effective_enabled=eff, generation=self.arb.desired_generation, ts=time.time())

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
        if not self.arb.master_inhibited and not getattr(s, "asleep", False) and requested:
            if self.arb.desired_latched:
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
        return {"master_inhibited": self.arb.master_inhibited, "generation": self.arb.desired_generation,
                "transition_epoch": self.arb.transition_epoch, "capabilities": caps, "ts": time.time()}

    def set_quiet(self, seconds: float) -> None:
        """'Shut up' — drop `say` for this many seconds (a temporary hush, distinct from the talk toggle)."""
        self._quiet_until = time.time() + max(0.0, seconds)

    def is_quiet(self) -> bool:
        return time.time() < self._quiet_until

    def check_say(self, s: Settings) -> Decision:
        if self.arb.master_inhibited:
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
