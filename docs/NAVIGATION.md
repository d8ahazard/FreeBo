# Navigation, mapping & "V-SLAM" on FreeBo (honest scope)

> For how the robot physically moves (the drivetrain, deadbands, the closed-loop motor control that makes
> movement reliable, and the nervous-system layering), see [MOTION.md](MOTION.md). This file covers the
> spatial/mapping side.

Enabot EBO robots (SE, Air, Air 2) are **monocular** — one camera, no LIDAR, no depth/ToF sensor, no wheel
odometry exposed to us. So true metric **V-SLAM** (a real floor-plan map + autonomous path planning) is not
achievable from our side, even though the commercial app advertises it. We don't fake it. Here's what FreeBo
actually offers, and why:

## What we provide (free, real)

- **Named places (manual mapping)** — the `places` skill. Drive FreeBo somewhere, `save_place("kitchen")`;
  it stores a reference camera snapshot **plus the rough VSLAM pose** at that spot. `list_places`, and
  `go_to_place` actually moves: each call takes ONE small, safety-clamped step toward the place — turning
  toward its saved bearing when a pose is available, otherwise nudging forward — and reports `arrived` once
  the live view matches the saved reference. The cortex calls it repeatedly and re-checks the camera.
- **Visual place-recognition** — `where_am_i` compares the live frame to saved places with a perceptual
  hash (Pillow). It answers "does this look like a place I know?" — the realistic stand-in for localization.
- **Topological place graph** — `data/places/graph.jsonl` is built automatically: whenever the robot saves
  a place, arrives at one, or confidently recognizes one (`where_am_i` high confidence), an edge is appended
  from the previous place to the current one. This records which places connect, for future path planning.
- **Spatial coverage / curiosity** — the brain feeds the VSLAM pose into a coarse visited-cell grid
  (`curiosity.py`) so it can nudge itself toward less-explored directions instead of circling one spot.
- **Collision avoidance** — the robot's **native** obstacle avoidance (`control/auto_avoidance`) and fall
  protection (`control/fallarrest`) are exposed as toggles, plus a non-LLM **ToF reflex** that stops the
  robot when something is too close (`AUTOBOT_REFLEX_STOP_CM`).

- **Pre-flight movement calibration** — `autobot/brain/motion_profile.py` + `POST /api/calibrate` (UI:
  Calibrate panel). In open space the robot does a few small test moves and measures the camera-view change
  (`frame_diff`, advisory VSLAM yaw) to derive a *controlled* step size + turn step + scene noise baseline,
  saved to `data/motion_profile.json`. The agent then sizes autonomous drive bursts from this profile
  (`_calib_drive`, `core._drive` caps forward bursts to the calibrated step) so it takes small observed steps
  instead of long blind lunges. Autonomy (`auto`) is gated until calibrated (`AUTOBOT_REQUIRE_CALIBRATION`,
  default on); manual control always works. This is relative (camera-change) tuning, not metric distance.

## What we don't pretend to do

- No metric map, no global path planning, no "clean the whole floor in a lawnmower pattern."
- `go_to_place` is best-effort visual + bearing navigation with small, observed moves — not guaranteed
  arrival. Monocular VSLAM pose is up-to-scale and drifts, so it's an advisory bearing, never ground truth.

## Why this is the right call

A robust monocular SLAM stack (e.g. ORB-SLAM3) needs camera calibration, a steady higher-FPS feed, and real
compute — impractical on a Pi from a low-FPS P2P stream, and fragile in homes. Named places + visual
recognition + native avoidance covers the genuinely useful behaviors (patrol, "go to the dock", "are you in
the kitchen?") without overpromising. If better hardware/sensors appear, this is the layer to extend.
