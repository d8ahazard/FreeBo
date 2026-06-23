"""Hard-coded motion model — the single source of truth for HOW each EBO robot physically moves.

Measured empirically via overseer puppet mode AND by sniffing the real EBO Home app's RTM control stream
(full write-up: docs/MOTION.md). The drivetrain is joystick-style: `ly` = forward/back, `rx` = in-place yaw
(`lx`/`ry` are ALWAYS 0 in the app — unused); the robot's firmware mixes those to its two treads.

What the app actually sends (id 101007, signed ints on a ~+/-100 scale, streamed at 10 Hz while moving, with
`buttons:1` = controller engaged on every frame incl. the zero/stop frame):

  * Forward = NEGATIVE ly. The app ramps ly smoothly; a full-stick hold settles around ly ~75-91.
  * Turning is PURE rx (ly stays 0) and is MODEST, not maxed: a gentle pivot is rx ~12-18, a firm turn
    ~25-42, and even the human's hardest turn only reached rx ~59. The old "turns must be fast / saturate
    instantly" assumption was WRONG — turns are clean, low-magnitude rx pulses.
  * Each axis still has a real DEADBAND (commands below it do nothing), and low-speed response is noisy
    run-to-run, so open-loop control is unreliable.

A single "speed" scalar can't serve both axes. Consistent motion comes from the closed loop in
locomotion.py (pulse -> measure the camera via visual_motion -> correct). These constants are the SEED
values that loop and the prompts use; the calibration profile (motion_profile.py) may refine them.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MotionModel:
    variant: str
    # --- deadbands: normalized command magnitude (0..1) below which NOTHING moves ---
    forward_deadband: float
    turn_deadband: float
    # --- usable command band (above the deadband, below "violent/uncontrollable") ---
    forward_min: float
    forward_max: float
    turn_min: float
    turn_max: float
    # --- recommended single-step seed units (the cerebellum starts here, then corrects) ---
    turn_unit_rx: float            # a small in-place pivot ~ STEP_TURN_DEG degrees
    turn_unit_duration: float
    forward_unit_speed: float      # a small, controlled forward step
    forward_unit_duration: float
    # rough turn rate (deg/s) at turn_max — used only to seed the closed loop's first pulse
    turn_deg_per_s: float
    # frame-diff motion gates (visual_motion, normalized 0..1): below still => didn't move; above move => moved
    still_diff: float
    move_diff: float
    # what self-sensing the robot actually has (Air 2 cloud stream exposes neither)
    has_tof: bool
    has_imu: bool


# A "small turn" the cerebellum aims for per pivot step, and a cap on any single AI-issued turn so the robot
# turns in re-checkable increments instead of whipping past openings.
STEP_TURN_DEG = 25.0
MAX_TURN_DEG = 75.0

# EBO Air 2 (cloud RTM/RTC): no ToF, no IMU in the stream. Bands below are anchored to the sniffed EBO Home
# app stream (gentle turn rx~0.12-0.18, firm ~0.25-0.42, human max ~0.59; forward hold settles ~0.75-0.91)
# plus overseer-measured deadbands (docs/MOTION.md). We stay on the gentle end and let the closed loop escalate.
AIR2 = MotionModel(
    variant="AIR2",
    forward_deadband=0.25, turn_deadband=0.10,
    forward_min=0.30, forward_max=0.55,
    turn_min=0.12, turn_max=0.45,
    turn_unit_rx=0.16, turn_unit_duration=0.30,
    forward_unit_speed=0.33, forward_unit_duration=0.50,
    turn_deg_per_s=90.0,
    still_diff=0.012, move_diff=0.030,
    has_tof=False, has_imu=False,
)

# EBO SE (LAN MAVLink): has a real 6-axis IMU + IR ToF, and a more linear response. We have not re-measured
# its deadbands here, so these mirror the historical calibration defaults (conservative) until calibrated.
SE = MotionModel(
    variant="SE",
    forward_deadband=0.15, turn_deadband=0.10,
    forward_min=0.30, forward_max=0.60,
    turn_min=0.20, turn_max=0.60,
    turn_unit_rx=0.45, turn_unit_duration=0.40,
    forward_unit_speed=0.50, forward_unit_duration=0.50,
    turn_deg_per_s=80.0,
    still_diff=0.012, move_diff=0.030,
    has_tof=True, has_imu=True,
)

_MODELS = {"AIR2": AIR2, "SE": SE}
# AIR/PRO route over the cloud plane like the Air 2; GENERIC falls back to the conservative Air 2 model.
_ALIASES = {"AIR": "AIR2", "PRO": "AIR2", "GENERIC": "AIR2"}


def for_variant(variant: str | None) -> MotionModel:
    v = (variant or "AIR2").upper()
    v = _ALIASES.get(v, v)
    return _MODELS.get(v, AIR2)


def guidance_text(variant: str | None) -> str:
    """Concise, intent-shaped movement guidance injected into every brain variant's prompt. It deliberately
    talks in INTENTS (turn a little / step forward), not raw magnitudes — the cerebellum (locomotion.py) owns
    the numbers, so the model never has to reason about deadbands or speeds."""
    m = for_variant(variant)
    sense = ("You have NO bumper, distance, or motion sensor — your CAMERA is your only sense of obstacles "
             "and of whether you actually moved." if not (m.has_tof or m.has_imu)
             else "You have a distance sensor and IMU, but your camera is still your main obstacle sense.")
    return (
        "HOW YOUR BODY MOVES (read this before driving):\n"
        f"- You roll on treads: you can go forward/back and PIVOT IN PLACE (turn without moving forward).\n"
        f"- {sense}\n"
        "- A low-level motor controller handles the exact speeds and takes only SHORT, confirmed steps — you "
        "just give SMALL, simple intents (a direction), so you don't need a perfectly clear scene to move.\n"
        "- DEFAULT TO MOVING: when there's open floor ahead (even a meter, with clutter only to the sides), "
        "drive FORWARD a short step to cover ground, then re-look. Don't just spin in place.\n"
        "- Turn left/right only when something is CLOSE directly ahead or you've reached a wall/dead end — "
        "then a small turn toward the open side, and re-look. Avoid long blind lunges."
    )
