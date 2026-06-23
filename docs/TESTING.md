# Testing & live diagnostics

Two layers, both aimed at one thing: never guess whether the robot actually works.

1. **Live capability self-test** — drives the *running app* and proves each capability against the real
   robot, with a clear PASS/FAIL/SKIP + evidence + a remediation hint.
2. **Mock-based regression suite** (`pytest`) — fast, hardware-free tests of the safety floor, skills,
   perception, the brain's VLM path, and the diagnostics logic. Runs in CI.

## 1. Live capability self-test

Run it while the app is up (`python -m autobot`). It uses the app's own robot session (it never opens a
second Agora session) and **always restores your settings + issues an e-stop** when it finishes.

```bash
python scripts/robot_selftest.py                 # full suite, no talk/hear playback (default)
python scripts/robot_selftest.py --no-move       # skip the driving checks (connection/video/eyes/vslam only)
python scripts/robot_selftest.py --talk --hear   # also play TTS on the speaker + ask you to speak
python scripts/robot_selftest.py --only connection,video,move
python scripts/robot_selftest.py --json > report.json
```

Or from the UI: the **Self-Test** panel (right rail) calls `GET /api/selftest` and shows the same rows.
Tick "motion" to include the driving checks.

### The capability matrix

| Check | What it proves | How |
|---|---|---|
| `connection` | robot online + session up | `/api/telemetry` connected/awake/battery/`via` |
| `video` | live camera frames flowing (not frozen) | two `/api/snapshot.jpg` + `video_frames` advancing |
| `eyes` | eye-expression control | `/api/control action eyes_happy` + telemetry echo |
| `move` | drives forward **and actually moves** | `move` then frame-diff + VSLAM pose delta → moved/blocked/stuck |
| `rotate` | turns in place + confirmed | `move` (yaw) then frame-diff + pose yaw |
| `talk` | speaks through the robot speaker | `/api/control say` (also checks TTS render) |
| `hear` | mic audio + STT transcript | audio frames flowing + `/api/diag/heard` after you speak |
| `autonomy` | one full autonomous decision cycle | `/api/tick` → the brain decides + drives, motion confirmed |
| `vslam` | visual odometry / mapping | `/api/slam/map` enabled + frames advancing |

`move`/`rotate`/`autonomy` only run with movement enabled. `talk`/`hear` only run with `--talk`/`--hear`
(or `?talk=1`). The `hear` check is interactive (you speak), so the `/api/selftest` endpoint never runs it.

### Motion confirmation (did it really move?)

`autobot/diagnostics/motion.py` combines two signals into `moved | blocked | stuck | unknown`:

- **frame-diff** — normalized mean-abs-diff of two downscaled grayscale frames (cv2 → Pillow → byte-size).
- **pose-delta** — VSLAM pose change (distance + yaw). Monocular + no Air 2 IMU, so it's odometry-grade
  (relative), used to corroborate the frame-diff, not as ground truth.

The brain uses the same primitives for its closed-loop check (see below), so the offline harness and the
live robot agree on what "moved" means.

## 2. Mock-based regression suite

```bash
pip install -r requirements-dev.txt
pytest                       # fast, hardware-free
pytest --hardware            # also run the live-robot tests against a running app (AUTOBOT_APP_URL)
```

Covers: `safety.py` clamps/gates/rate-limit, the core skill, perception, the diagnostics check + motion
logic (against a fake app client), and the brain's VLM reason path (mocked vision service + `MockRobotLink`,
including that a drive arms the closed-loop motion check).

CI (`.github/workflows/ci.yml`) runs the protocol byte-identity gate (`scripts/eboproto_check.py`) and the
suite on pushes/PRs.

## Closed-loop motion confirmation (in the brain)

With `confirm_motion` on (default; `AUTOBOT_CONFIRM_MOTION`), after the AI issues a move the brain records
the frame + VSLAM pose, and on the next cycle classifies whether it actually moved. If a forward move comes
back `stuck`, the brain turns instead of pushing into the same wall, emits a `motion` event for the UI, and
exposes `motion_state` in the brain status. Fail-soft: any error disables it for that cycle.
