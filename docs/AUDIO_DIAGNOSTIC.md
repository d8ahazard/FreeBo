# Phase 0.3 — Air 2 listening diagnostic (findings of record)

> **STATUS: COMPLETE — captured live on the Air 2, 2026-06-24.** Raw evidence:
> [`data/test-evidence/audio_calibration.json`](../data/test-evidence/audio_calibration.json) (6 windows via
> the UI Calibrate tab). Final adaptive-VAD constants are set in `.env` and recorded below.

This is the **traceability artifact** for the Phase 0.4 adaptive-VAD thresholds. The adaptive noise-floor
clamps and enter/exit constants are derived from the measurements recorded here, not tuned by feel.

The listening pipeline under test is the native path:
robot mic → Agora RTC → `AgoraNativeReceiver` (16 kHz PCM) → `MediaHub` → `AudioSink` → `brain.feed_speech`.
The legacy 2.5 s `skills/voice.py` chunker is NOT on this path.

## How to run

1. Start the app on the deployment box against the real robot:
   `AUTOBOT_ROBOT_LINK=air2_native AUTOBOT_AI_PROVIDER=vlm python -m autobot`
2. With the room quiet, poll the new per-stage surface and record the idle (silence) numbers:
   `GET /api/diag/audio` → `audio_sink.max_rms`, `recv`, `chunks`, `drop_*`.
3. Speak short commands at a normal distance/volume ("FreeBo, stop", "turn left", "what do you see").
   Re-poll `/api/diag/audio` (RMS during speech, `vad_starts/ends`, `seg_accepted/seg_dropped_short`,
   `last_stt_ms`, `last_queue_wait_ms`, `recent[]`) and `GET /api/diag/heard` (transcripts).
4. Capture a few raw utterance WAVs for intelligibility (operator: save from the publish/STT tap) and note
   whether Whisper transcribed them correctly.

## Measurements (live Air 2, 2026-06-24; RMS = AudioSink window distribution)

Captured via the UI **Calibrate** tab (per-window `POST /api/diag/audio/reset` → read → `…/capture`). STT ran
on **GPU** (`cuda:float16:base.en`).

| Window | RMS p50 | RMS p90 | RMS p95 | RMS max | vad starts/ends | accepted/dropped | STT p50 ms | transcripts |
|---|---|---|---|---|---|---|---|---|
| silence (idle) | 156 | 158 | 159 | 3252* | 1 / 2 | 2 / 0 | 219 | — (filtered) |
| normal @1m | 157 | 3847 | 5337 | 10370 | 2 / 1 | 0 / 1 | — | **0 (over-segmented)** |
| quiet | 155 | 1090 | 1680 | 2871 | 2 / 1 | 1 / 0 | — | — |
| loud | 2176 | 17192 | 20550 | 28093 | 1 / 0 | 0 / 0 | — | — |
| room_noise | 155 | 7340 | 8570 | 11527 | 1 / 2 | 2 / 0 | 148 | "8, 9, 10." ✓ |
| tts_playback (self-echo) | 160 | 463 | 702 | 4658 | 3 / 2 | 2 / 0 | — | — (filtered) |

\* idle p99 ~900 / max 3252 are rare single-chunk transients (filtered by `min_speech_s`, not real speech).

Idle floor ≈ **158 RMS** (steady p50/p90). Even **quiet** speech energy (~1000+) is ~6× idle. STT on GPU is
**~150–300 ms/utterance** with **0 queue wait** (vs ~14 s on CPU pre-fix).

## Failure attribution (conclusions)

- [x] **STT was slow (FIXED)** — CPU `base.en` ~14 s/utt + 22 s backlog → moved STT to **GPU** (`cuda:float16`),
  now ~150–300 ms. Required dropping the cortex 14b→7b to free VRAM (temporary; see `.env`).
- [x] **Segments dropped on paused speech** — `normal_1m` produced 0 transcripts (words separated by sub-
  threshold gaps end the segment at `hang_s=0.7`, then drop as too-short). **Fix:** `AUTOBOT_STT_HANG` 0.7 → 0.8.
- [x] **Self-echo present** — `tts_playback` robot voice reached ~700–4658 RMS and tripped VAD 3× with
  `drop_speaking=0`: the manual `/api/control say` button does NOT arm the echo gate (the brain's `say` tool
  does, via SpeechService). FOLLOW-UP for the barge-in gate (route control-say through SpeechService).
- [x] **No "no-audio" / "VAD-never-fires" failures** — `recv` advanced in every window; VAD fired on speech.

## Final adaptive-VAD constants (set in `.env`, from the data above)

| Constant (env) | Final | Rationale (from data) |
|---|---|---|
| `AUTOBOT_STT_RMS_MIN` (floor clamp low) | **150** | at/below the measured idle floor (~158) |
| `AUTOBOT_STT_RMS_MAX` (floor clamp high) | **500** | well below quiet-speech energy (~1000) so the floor can never learn speech as noise |
| `AUTOBOT_STT_ENTER_K` (enter = K·floor) | **2.5** | enter ≈ 395 at floor 158 — above idle, below quiet speech (~1000) |
| `AUTOBOT_STT_EXIT_K` (exit = K·floor) | **1.5** | exit ≈ 237 — hysteresis below enter |
| `AUTOBOT_STT_HANG` (utterance end-gap) | **0.8** | 0.7 over-segmented paused command speech |
| `AUTOBOT_STT_RMS` (fixed override) | unset | removed → adaptive engages; set =250 to revert to the validated fixed gate |

> The fixed `RMS=250` gate was also validated by the data (it sits between idle 158 and quiet speech 1000),
> but the adaptive floor is more robust to room-condition changes and is the chosen production setting.
