# Motion system

How FreeBo actually moves, why naive driving fails, and the layered "nervous system" that makes movement
reliable. This is hard-coded knowledge: the constants live in
[autobot/brain/motion_model.py](../autobot/brain/motion_model.py) and the closed loop in
[autobot/brain/locomotion.py](../autobot/brain/locomotion.py). Measured empirically via overseer puppet mode
(`config.overseer`, see [SAFETY.md](SAFETY.md)).

## The drivetrain (what the robot understands)

Every drive is a joystick-style frame `{lx, ly, rx, ry}` (see
[autobot/robot/frames.py](../autobot/robot/frames.py) / `scripts/rtm_sidecar.js`):

- `ly` = forward/back (robot negates internally; `ly>0` = forward).
- `rx` = **in-place yaw** (pivot; `rx>0` = right, `rx<0` = left).
- `lx`, `ry` = unused (always 0). The robot's firmware mixes `ly`+`rx` onto its two treads — we do **not**
  command wheels individually.

So the full motion space is reachable as (throttle, yaw): pure pivot = `ly=0, rx=+/-`, arc = both.

**Wire format (sniffed from the real EBO Home app, RTM id `101007`).** Values are **signed ints on a ~+/-100
scale**, streamed at **~10 Hz** while moving, each frame carrying **`buttons:1`** — the "controller engaged"
flag. The robot **ignores joystick frames sent with `buttons:0`**, so our sidecar sets `buttons:1` on every
frame including the zero/stop frame. `lx`/`ry` are always `0`. Forward = **negative** `ly`. The app *ramps*
the magnitude smoothly rather than jumping to the target.

## Why naive driving crashes (the measured reality, EBO Air 2)

Measured live; numbers are normalized command magnitude 0..1.

- **Forward deadband:** nothing moves below `ly ~= 0.25`; motion starts ~`0.30`. The app's full-stick
  forward hold settles around `ly ~= 0.75–0.91` (normalized), so there is lots of headroom above our
  conservative `forward_max = 0.55`.
- **Turn deadband:** nothing below `rx ~= 0.10`. **Sniffing the real app corrected our earlier assumption
  that turns must be fast/maxed:** the app turns with *modest* pure-`rx` pulses — gentle pivot `rx ~= 0.12–0.18`,
  firm turn `~0.25–0.42`, and even the human's hardest turn only reached `rx ~= 0.59`. We seed at `rx = 0.16`
  and cap at `turn_max = 0.45`, letting the closed loop escalate within that band.
- **Axis mismatch:** forward needs a bigger push to start moving than a turn does, and a single `speed`
  scalar (the old `drive` tool default `speed=0.8, duration=1.2`) cannot serve both — applied to `rx` that
  is a large over-rotation. This was the root cause of "it turns into walls." The cerebellum now drives each
  axis from its own band.
- **Non-linear + noisy:** low-speed response varies run-to-run, so **open-loop control is unreliable**.
- **No proprioception:** the Air 2 exposes **no IMU and no ToF** in its cloud stream
  ([NATIVE_AIR2.md](NATIVE_AIR2.md)), so there is no self-motion sense and the ToF reflex never fires.
- **VSLAM lies during motion** (300deg+ yaw deltas while physically stationary) — see [NAVIGATION.md](NAVIGATION.md).

The one reliable signal is the **camera**: frame-diff (~0.005 still, >0.03 real motion) and phase-correlation
horizontal shift for small in-place yaw. That is the basis of all closed-loop motion here.

## The nervous-system layers

From reflex (fast, dumb, safe) up to reasoning (slow, smart). Lower layers never depend on the GPU.

1. **Sensing** — [visual_motion.py](../autobot/brain/visual_motion.py): camera -> `diff`, `shift_x`,
   `est_yaw_deg`. The robot's proprioception substitute. CPU/OpenCV.
2. **Reflex (brainstem)** — `reflex_vision.py` looming detector -> emergency stop, replacing the dead ToF
   reflex on the Air 2. Plus the native-link deadman. CPU.
3. **Cerebellum (motor control)** — [locomotion.py](../autobot/brain/locomotion.py): turns high-level
   intents (`turn_to(deg)`, `step()`) into deadband-aware pulses and uses `visual_motion` feedback to hit the
   target and confirm it moved. The fix for consistency. CPU.
4. **Safety floor** — [safety.py](../autobot/brain/safety.py): unchanged mechanical clamps (`max_speed`,
   `max_move_duration`, rate limit, autonomy/scope gating). Everything passes through it.
5. **Spatial sense** — [slam.py](../autobot/brain/slam.py): advisory pose, hardened to fail safe (gated,
   clamped, frozen-when-still). Never ground truth.
6. **Reasoning cortex** — the LLM/VLM, which emits **intents** (turn a little, step forward) and never raw
   magnitudes. One GPU vision model; reasoning is swappable (local-small or cloud).

```
camera -> visual_motion -> reflex_vision -> safety -> RobotLink
                        \-> locomotion (cerebellum) -> safety -> RobotLink
                        \-> slam (advisory)
cortex (intents) ------> locomotion
motion_model (constants) -> locomotion + cortex prompt
```

## The motion model constants

[autobot/brain/motion_model.py](../autobot/brain/motion_model.py) is the single source of truth, per variant
(`AIR2`, `SE`, with `AIR`/`PRO`/`GENERIC` aliased to the conservative Air 2 model). It carries per-axis
deadbands, usable bands, seed step units, a rough turn rate (deg/s), and the frame-diff gates, plus
`guidance_text(variant)` — the intent-shaped movement instructions injected into every brain variant's
prompt. The calibration profile ([motion_profile.py](../autobot/brain/motion_profile.py)) may refine these
per robot/scene and seeds itself from the model when uncalibrated (`MotionProfile.from_model`).

`STEP_TURN_DEG` (≈25deg) is the target per pivot step; `MAX_TURN_DEG` (≈75deg) caps any single AI turn so the
robot turns in re-checkable increments instead of overshooting.

## Rules for adding/altering motion

- Never send raw `ly/rx` from the brain — go through `locomotion`. The brain speaks in intents.
- Any new motion path still passes through `safety.check_drive`.
- Update the constants here (and `motion_model.py`) in the same change, and keep this doc in sync.
- The `moveSpeed`/`moveMode` firmware gear (RTM 103011) changes responsiveness but not as a clean monotonic
  gear; a real low-gear `moveSpeed` set needs the EBO app protocol sniffed — deferred, the cerebellum makes
  us robust without it.
