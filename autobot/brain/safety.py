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

# P0 (agent_next_2 §1): the closed set of physical effect classes. EVERY non-zero robot effect is one of these
# and must carry an admitted EffectTicket. E-STOP and zero/deadman motion are the only always-permitted effects
# (they are not admitted as tickets). Read-only telemetry / operator video are NOT effects.
EFFECT_MOTION = "motion"
EFFECT_DOCK = "dock"
EFFECT_RELEASE = "release"            # give up controller ownership (robot autonomy)
EFFECT_RESUME = "resume"             # re-claim controller ownership
EFFECT_MOVE_MODE = "move_mode"
EFFECT_MOVE_SPEED = "move_speed"
EFFECT_AVOID = "avoid"
EFFECT_LASER = "laser"
EFFECT_EYES = "eyes"
EFFECT_SPEECH = "speech"
EFFECT_CALIBRATION = "calibration"
EFFECT_CLASSES = (EFFECT_MOTION, EFFECT_DOCK, EFFECT_RELEASE, EFFECT_RESUME, EFFECT_MOVE_MODE,
                  EFFECT_MOVE_SPEED, EFFECT_AVOID, EFFECT_LASER, EFFECT_EYES, EFFECT_SPEECH,
                  EFFECT_CALIBRATION)


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    # normalized/clamped values the caller should actually use:
    ly: float = 0.0
    rx: float = 0.0
    duration: float = 0.0


@dataclass
class StopToken:
    """Identity of ONE master-STOP dispatch (P0-R4 atomicity item 1). Tracked while its transport send is in
    flight; only this exact token's completion clears it, so an older STOP finishing can't make a newer STOP
    look done."""
    epoch: int
    generation: int
    dispatch_id: int


@dataclass
class ResetToken:
    """A single-use reservation captured when a RESET is ADMITTED (agent_next_2 §2.1). It reserves a brand-new
    post-resume (release_epoch, release_generation) — strictly newer than the current state — so commands
    admitted before the completed RESUME stay stale forever. The reset finalizes only if it is still the active
    attempt, no STOP is in flight, and the current (epoch, generation) still equal the expected ones. The
    process/sidecar instances + the sidecar prepare nonce are tracked by the orchestration layer (RtmNode)."""
    reset_attempt_id: int
    expected_epoch: int
    expected_generation: int
    release_epoch: int
    release_generation: int


@dataclass
class EffectTicket:
    """An admission ticket for ONE physical robot effect (agent_next_2 §1.1). Carries the control transition it
    was admitted under (epoch, generation), the effect class, and a unique ticket id. Re-validated immediately
    before the write reaches the sidecar and a THIRD time inside the sidecar — so an effect admitted before a
    STOP cannot dispatch after it, at any hop. Motion uses `effect_class=motion` (the `MotionTicket` alias)."""
    epoch: int
    generation: int
    effect_class: str = EFFECT_MOTION
    ticket_id: int = 0


# Motion is just the motion-class effect ticket. Kept as a name for the motion call sites + existing tests.
MotionTicket = EffectTicket


class ControlArbiter:
    """The ONE process-side control-transition authority (P0-R4 atomicity). Every STOP / RESET / motion
    admission consults this single RLock-protected object. ALL access is via lock-protected methods — callers
    must never read mutable fields directly (item 9)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._epoch = 0
        self._generation = 0
        self._latched = False
        self._inhibited = False
        self._active_stops: set[int] = set()   # dispatch_ids of STOPs whose transport send is in flight
        self._dispatch_seq = 0
        self._reset_seq = 0
        self._active_reset_id: Optional[int] = None
        self._effect_seq = 0          # monotonic ticket-id source for admitted effects

    # --- transitions ---
    def begin_master_stop(self) -> StopToken:
        """STOP: assert inhibit + latch, ALWAYS advance epoch + generation, register a tracked in-flight
        dispatch, and invalidate any in-progress reset admission. Returns this STOP's unique token."""
        with self._lock:
            self._epoch += 1
            self._generation += 1
            self._latched = True
            self._inhibited = True
            self._dispatch_seq += 1
            did = self._dispatch_seq
            self._active_stops.add(did)
            self._active_reset_id = None    # a new STOP cancels any pending reset admission
            return StopToken(self._epoch, self._generation, did)

    def latch_motion(self) -> StopToken:
        """Motion-only latch (reflex/barge-in). Monotonic; NOT a tracked master dispatch (dispatch_id 0)."""
        with self._lock:
            self._epoch += 1
            self._generation += 1
            self._latched = True
            return StopToken(self._epoch, self._generation, 0)

    def end_estop_dispatch(self, token: StopToken) -> None:
        """Clear ONLY this STOP's in-flight marker."""
        with self._lock:
            self._active_stops.discard(token.dispatch_id)

    def stop_in_flight(self) -> bool:
        with self._lock:
            return bool(self._active_stops)

    # --- reset admission + finalization (agent_next_2 §2.1/§2.4) ---
    def begin_reset(self) -> Optional[ResetToken]:
        """Admit a reset ONLY when safe to even contact the link: no STOP in flight, currently latched +
        inhibited, and no other reset already active. Reserves a brand-new post-resume (release_epoch,
        release_generation). Returns None (rejected) otherwise. Releases NO faculty."""
        with self._lock:
            if self._active_stops:
                return None
            if not (self._inhibited and self._latched):
                return None
            if self._active_reset_id is not None:
                return None
            self._reset_seq += 1
            self._active_reset_id = self._reset_seq
            return ResetToken(self._reset_seq, self._epoch, self._generation,
                              self._epoch + 1, self._generation + 1)

    def finalize_reset(self, token: ResetToken) -> bool:
        """Process finalization (§2.4): the single-use atomic install of the reserved release state. Consumes the
        token on EVERY path so it can never finalize twice. Finalizes only if it is the active attempt, nothing
        is in flight, and the current (epoch, generation) still equal the expected pre-reset values. On success
        installs the reserved release epoch/generation and clears the latch + master inhibit."""
        with self._lock:
            if token.reset_attempt_id != self._active_reset_id:
                return False                 # not the active attempt / already consumed
            self._active_reset_id = None      # consume
            if self._active_stops:
                return False
            if token.expected_epoch != self._epoch or token.expected_generation != self._generation:
                return False                 # a STOP (or anything) advanced state since admission
            self._epoch = token.release_epoch
            self._generation = token.release_generation
            self._latched = False
            self._inhibited = False
            return True

    # Back-compat alias: the process finalize used to be called commit_reset.
    def commit_reset(self, token: ResetToken) -> bool:
        return self.finalize_reset(token)

    def abort_reset(self, token: ResetToken) -> None:
        with self._lock:
            if token.reset_attempt_id == self._active_reset_id:
                self._active_reset_id = None

    # --- effect / motion admission (item 8 + agent_next_2 §1.1) ---
    def admit_effect(self, effect_class: str) -> Optional[EffectTicket]:
        """Admit ONE physical effect: a unique-ticket-id snapshot of the current transition, ONLY when not
        inhibited/latched and no STOP is in flight. Mechanical gate only — per-class POLICY (autonomy, talk
        toggle, etc.) is applied by SafetyFloor.admit_effect before calling this."""
        with self._lock:
            if self._inhibited or self._latched or self._active_stops:
                return None
            self._effect_seq += 1
            return EffectTicket(self._epoch, self._generation, effect_class, self._effect_seq)

    def admit_motion(self) -> Optional[EffectTicket]:
        return self.admit_effect(EFFECT_MOTION)

    def validate_ticket(self, ticket: EffectTicket) -> bool:
        """A ticket is valid only while the control transition it was admitted under is still current and motion
        is permitted (not inhibited/latched, no STOP in flight). Epoch+generation must match exactly."""
        with self._lock:
            return (not self._inhibited and not self._latched and not self._active_stops
                    and ticket.epoch == self._epoch and ticket.generation == self._generation)

    # --- lock-protected reads (item 9) ---
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def is_latched(self) -> bool:
        with self._lock:
            return self._latched

    def is_master_inhibited(self) -> bool:
        with self._lock:
            return self._inhibited

    def snapshot(self) -> dict:
        with self._lock:
            return {"transition_epoch": self._epoch, "desired_generation": self._generation,
                    "desired_latched": self._latched, "master_inhibited": self._inhibited,
                    "stop_in_flight": bool(self._active_stops),
                    "reset_active": self._active_reset_id is not None}

    def _unsafe_clear_for_tests(self) -> None:
        """TEST/no-sidecar ONLY — never a production release path (item 10)."""
        with self._lock:
            self._latched = False
            self._inhibited = False
            self._active_stops.clear()
            self._active_reset_id = None


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
        snap = self.arb.snapshot()
        if snap["master_inhibited"]:
            return Decision(False, "master_inhibited (STOP)")
        if snap["desired_latched"]:
            return Decision(False, "estop_latched")
        if snap["stop_in_flight"]:
            return Decision(False, "estop dispatch in flight")
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
        return self.arb.latch_motion().generation

    # --- the single STOP/RESET transition entries (P0-R4 atomicity) ---
    def begin_master_stop(self) -> "StopToken":
        """The ONE synchronous master-STOP transition. Returns a StopToken (epoch, generation, dispatch_id)."""
        return self.arb.begin_master_stop()

    def end_estop_dispatch(self, token: "StopToken") -> None:
        self.arb.end_estop_dispatch(token)

    def begin_reset(self) -> Optional[ResetToken]:
        """Admit a reset (None if rejected — STOP in flight / not latched / another reset active)."""
        return self.arb.begin_reset()

    def finalize_reset(self, token: ResetToken) -> bool:
        """Process finalization of a two-phase release (§2.4): atomically install the reserved release state."""
        return self.arb.finalize_reset(token)

    def commit_reset(self, token: ResetToken) -> bool:
        return self.arb.commit_reset(token)   # back-compat alias for finalize_reset

    def abort_reset(self, token: ResetToken) -> None:
        self.arb.abort_reset(token)

    def admit_motion(self) -> Optional[MotionTicket]:
        return self.arb.admit_motion()

    def admit_effect(self, effect_class: str, source: str = "ai",
                     settings: Optional[Settings] = None) -> Optional[EffectTicket]:
        """The ONE authority for admitting a non-zero physical robot effect (agent_next_2 §4.2). Applies
        per-class POLICY on top of the mechanical arbiter gate (master inhibit / latch / STOP in flight). Returns
        an EffectTicket carrying the current (epoch, generation, effect_class, ticket_id) or None (denied).
        E-STOP and zero/deadman motion are NOT admitted here — they are always permitted by their own path."""
        s = settings if settings is not None else getattr(self, "_settings_ref", None)
        # Speech is additionally gated by the talk toggle + quiet window (mirrors check_say's policy view).
        if effect_class == EFFECT_SPEECH:
            if s is not None and not bool(getattr(s, "talk_enabled", False)):
                return None
            if self.is_quiet():
                return None
        # All other effect classes rely on the master-inhibit/latch/STOP gate in the arbiter (motion's extra
        # speed/scope/autonomy policy is applied by check_drive before admission).
        ticket = self.arb.admit_effect(effect_class)
        # Phase 1 observability (agent_next_3 §C3): record every effect admission decision (grant/deny).
        try:
            from .. import observability as _obs
            if ticket is None:
                _obs.emit(_obs.CAT_EFFECT, effect_class, source, requested="admit", effective="denied",
                          reason="inhibited/latched/stop-in-flight")
            else:
                _obs.emit(_obs.CAT_EFFECT, effect_class, source, requested="admit", effective="admitted",
                          outcome="ticket", epoch=ticket.epoch, generation=ticket.generation,
                          ticket_id=ticket.ticket_id)
        except Exception:  # noqa: BLE001 - observability never breaks admission
            pass
        return ticket

    def validate_ticket(self, ticket: EffectTicket) -> bool:
        return self.arb.validate_ticket(ticket)

    def stop_in_flight(self) -> bool:
        return self.arb.stop_in_flight()

    def transition_epoch(self) -> int:
        return self.arb.epoch()

    def is_latched(self) -> bool:
        return self.arb.is_latched()

    def control_generation(self) -> int:
        return self.arb.generation()

    # --- master autonomous-faculty inhibit ---
    def is_master_inhibited(self) -> bool:
        return self.arb.is_master_inhibited()

    # NOTE (P0-R4 atomicity item 10): there is intentionally NO public unconditional master_inhibit /
    # master_release / estop_reset / force_clear. STOP goes through begin_master_stop() (tokenized); release
    # happens ONLY through the reconciled CAS commit_reset(). Tests use arb._unsafe_clear_for_tests().

    # --- central faculty authority (P0-R4.1) ---
    def _faculty(self, capability: str, requested: bool, s: Settings,
                 extra_reason: str = "") -> FacultyDecision:
        """Common faculty decision: master inhibit > asleep > requested toggle > faculty-specific extra."""
        snap = self.arb.snapshot()
        if snap["master_inhibited"]:
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
                               master_inhibited=snap["master_inhibited"], requested_enabled=requested,
                               effective_enabled=eff, generation=snap["desired_generation"], ts=time.time())

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
        snap = self.arb.snapshot()
        if not snap["master_inhibited"] and not getattr(s, "asleep", False) and requested:
            if snap["desired_latched"]:
                extra = "estop_latched"
            elif snap["stop_in_flight"]:
                extra = "estop dispatch in flight"
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
        snap = self.arb.snapshot()
        return {"master_inhibited": snap["master_inhibited"], "generation": snap["desired_generation"],
                "transition_epoch": snap["transition_epoch"], "stop_in_flight": snap["stop_in_flight"],
                "capabilities": caps, "ts": time.time()}

    def set_quiet(self, seconds: float) -> None:
        """'Shut up' — drop `say` for this many seconds (a temporary hush, distinct from the talk toggle)."""
        self._quiet_until = time.time() + max(0.0, seconds)

    def is_quiet(self) -> bool:
        return time.time() < self._quiet_until

    def check_say(self, s: Settings) -> Decision:
        if self.arb.is_master_inhibited():
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
