# Safety

Autobot drives a real motorized robot with a speaker and an autonomous AI in the loop. Safety is mechanical,
not just prompt-based: the model is *asked* to behave, but the system *enforces* limits regardless.

## The safety floor (`autobot/brain/safety.py`)

Every robot-affecting action passes through `safety.py` before the `RobotLink`. It enforces:

1. **Speed clamp** — drive vector magnitude is clamped to `config.max_speed` (0..1). The AI cannot exceed
   the user's setting.
2. **Duration cap** — timed moves are capped (default 1.5s) so a single command can't run away.
3. **Rate limit** — at most N motion actions per tick; extra calls are dropped with a logged reason.
4. **Talk gate** — `say` is dropped unless `config.talk_enabled` is true (UI toggle, default OFF).
5. **Autonomy gate** — in `manual`, AI motion is dropped; only the UI's manual controls drive.
6. **Fail safe** — any exception in perceive/think/act results in a stop, not continued motion.

## Two-layer deadman

- **Brain layer:** the agent loop only sends motion in short bursts and stops between ticks.
- **Native-link layer:** `NativeRobotLink` runs its own watchdog (`_drive_watchdog`) — if `drive` frames
  stop arriving for ~0.35s it sends a motor-stop frame regardless of what the brain is doing. This survives
  a wedged agent loop or a crashed provider call. **Do not remove it.**

## ToF reflex (added safety layer)

In addition to the deadman, a non-LLM reflex (`agent._reflex_loop`) watches the cached ToF/IR distance and
issues a stop the instant an obstacle is closer than `AUTOBOT_REFLEX_STOP_CM` (default 18cm; 0 disables),
then arms a "turn, don't push forward" hint for the next decision. It complements — never replaces — the
deadman and onboard avoidance.

## New motion paths still go through the floor

Any tool that moves the robot routes through `safety.check_drive` like everything else: `go_to_place`
(places skill) takes small, clamped steps; scheduled-task directives (`feed_task`) and MCP-triggered actions
are ordinary reasoning that still hit the floor. Owner-authority gating applies to `drive`, `dock`,
`go_to_place`, `save_place`, Home Assistant control, scheduling, and (per-server) MCP tools.

## Emergency stop

The UI has a always-visible STOP button. It:

- calls `/api/estop`, which stops the robot via the `RobotLink` **directly** (not through the agent loop), and
- forces `autonomy = manual` so the AI won't immediately re-drive.

It must keep working even if the AI loop or provider is hung.

## Go dark (Sleep) — full kill switch

The UI's Sleep button calls `POST /api/sleep {on:true}` (`_set_dark`), a stronger stop than estop: it stops
the robot, sets `asleep=true` + `autonomy=manual`, and calls `LINK.connection("stop")` which (on
`air2_native`) stops drive, **releases controller authority**, and **drops all inbound media to the hub** so
the brain perceiver, captioner, STT, SLAM, and MJPEG all go quiet — while keeping the Agora session warm.
While dark, `/api/tick`, `/api/chat`, autonomous reasoning, `feed_speech`, and manual drive/say/action are
all refused; only estop, wake, and `stop` still work. Wake (`{on:false}`) reverses it: re-acquire control,
re-enable the mic, clear `asleep`, and restore the prior autonomy mode. estop remains the fast panic-halt;
Sleep is the "cut everything" switch.

## Overseer puppet mode (`config.overseer`)

A diagnostic/calibration mode that **paralyzes the AI brain** without stopping it. When `overseer` is on,
the brain still perceives, thinks, and emits tool calls, but it reaches the robot only through
`OverseerGate` (`autobot/robot/overseer_gate.py`), which **intercepts every robot-affecting verb**
(`drive`/`move`/`stop`/`action`/`say_*`/`publish_speech`): each is recorded as a *proposal* and a synthetic
`{"ok":true,"intercepted":true}` is returned, so the brain believes it acted while **nothing reaches the
robot**. A human/agent overseer then drives the real robot directly through `POST /api/overseer/act` and
watches the brain's intent + live state via `GET /api/overseer/state`. When `overseer` is off the gate is a
transparent passthrough — normal operation is unchanged.

Overseer commands use `source="overseer"` in `safety.check_drive`: like `manual`, they are **clamped to
`max_speed` + `max_move_duration`** but are **not** autonomy-gated or rate-limited (the human is the
operator). The speed/duration clamp is still non-negotiable. `say` via overseer still respects
`talk_enabled`. The gate is the single chokepoint for the "paralysis" because the brain only ever touches
the robot through a `RobotLink`.

## Movement scope (behavior gate)

On top of the speed/duration/calibration clamps, every AI drive is gated by a per-cycle **movement scope**
set by the behavior controller (`safety.set_scope`): `roam` (normal), `adjust` (rotate in place only — the
translation axis is zeroed), or `hold` (AI motion blocked entirely). The robot OBSERVES by default (adjust)
and only roams for a reason (explore mode, greeting a person, idle patrol, a command, or a voice order), so
it no longer wanders/backs into things every tick. Manual control bypasses scope (the human is in control).

## Spoken commands may change user settings (owner-gated)

By design the AI cannot change user-only settings. The exception is **spoken owner commands**: voice can
switch mode, start/stop roaming (autonomy), dock, go dark (sleep), and quiet talk. STOP / QUIET / SLEEP /
SPEAK_UP / BACK_UP are always honored; mode/roam/home/come-here are gated to the owner (via `identity`) when
owner-only obedience is on. The mechanical floor is unchanged: `max_speed`, the deadman, estop, the talk
toggle default, and the scope clamp still bound everything the AI can do.

## Closed-loop motion confirmation (`confirm_motion`)

After the AI issues a move, the brain checks whether the robot *actually* moved (camera frame-diff + VSLAM
pose) and, if a forward move came back `stuck`, turns instead of repeatedly driving into the same obstacle.
It is fail-soft (any error just skips the check that cycle) and never causes motion on its own — it only
makes the AI's *own* next decision smarter. Toggle with `AUTOBOT_CONFIRM_MOTION` (default on). See
[TESTING.md](TESTING.md).

## Self-test safety

The capability self-test (`scripts/robot_selftest.py` / `GET /api/selftest`) drives the live robot, so it is
built to fail safe: motion bursts are short and pass through the same speed/duration clamps as any manual
move, and the runner **always restores your settings and issues an emergency stop** when it finishes
(autonomy ends in `manual`). The interactive `hear` check is never triggered from the API.

## User-only controls (the AI cannot change these)

`max_speed`, `talk_enabled`, `autonomy`, and the goal are set by the human in the UI. The AI has no tool to
change them. This is enforced by the tool set, not by trust.

## Operating guidance

- Keep the robot in a safe area; supervise `auto` runs, especially near stairs (enable `fall` protection).
- Start with a low `max_speed` (0.4–0.6) and short `tick_seconds`.
- Talk OFF by default; enable only when you want the robot to speak.
- The app holds a single robot session; use the UI "release" control to hand the robot back to the Enabot app.

## Adding capabilities safely

If a new tool can cause motion, sound, or a physical state change, add its constraint to `safety.py` in the
same change and document it here. No exceptions.
