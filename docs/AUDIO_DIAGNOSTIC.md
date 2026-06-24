# Phase 0.3 — Air 2 listening diagnostic (findings of record)

This is the **traceability artifact** for the Phase 0.4 adaptive-VAD thresholds. The adaptive noise-floor
clamps and enter/exit constants must be derived from the measurements recorded here, not tuned by feel. Until
a hardware run fills the tables, the Phase 0.4 defaults are **provisional** and marked as such in
[`autobot/brain/audio_sink.py`](../autobot/brain/audio_sink.py).

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

## Measurements (PENDING HARDWARE RUN — fill from step 2-4)

| Quantity | Source field | Quiet room | Normal speech | Notes |
|---|---|---|---|---|
| RMS floor (idle) | `max_rms` after 10 s silence | _TBD_ | — | baseline noise |
| RMS during speech | `max_rms` while speaking | — | _TBD_ | |
| Packets received | `recv` / `chunks` | _TBD_ | _TBD_ | confirms audio flows at all |
| `drop_speaking` | echo-gate drops | _TBD_ | _TBD_ | should be ~0 unless TTS active |
| VAD starts/ends | `vad_starts` / `vad_ends` | should be 0 | should match #utterances | |
| Accept vs drop | `seg_accepted` / `seg_dropped_short` | — | _TBD_ | drops = too-short tuning |
| STT latency | `last_stt_ms` | — | _TBD_ | CPU base.en budget |
| Queue wait | `last_queue_wait_ms` | — | _TBD_ | backlog indicator |
| Transcript accuracy | `/api/diag/heard` | — | _TBD_ | intelligibility verdict |

## Failure attribution (fill the conclusion)

- [ ] **No audio** — `recv`/`chunks` ~0 → media/mic handshake (RTC recv-audio / 102001-102003), not VAD.
- [ ] **VAD never fires** — `recv` high but `vad_starts` 0 → threshold too high vs speech RMS (→ 0.4 floor).
- [ ] **VAD over-fires** — `vad_starts` high on silence → threshold too low / noisy room (→ 0.4 floor + hysteresis).
- [ ] **Segments dropped** — `seg_dropped_short` high → `min_speech_s` / sample-length gate too strict.
- [ ] **STT slow** — `last_stt_ms` or `last_queue_wait_ms` large → model/device (CPU base.en) budget.
- [ ] **Hallucination/echo** — phantom transcripts or self-echo → filter + barge-in self-echo rejection (0.5).

## Derived Phase 0.4 constants (set after the run)

| Constant (env) | Provisional default | Final (from data) | Rationale |
|---|---|---|---|
| `AUTOBOT_STT_RMS_MIN` (floor clamp low) | 250 | _TBD_ | must stay above idle noise floor |
| `AUTOBOT_STT_RMS_MAX` (floor clamp high) | 1500 | _TBD_ | never learn speech-level as noise |
| `AUTOBOT_STT_ENTER_K` (enter = K·floor) | 2.5 | _TBD_ | speech RMS / idle floor ratio |
| `AUTOBOT_STT_EXIT_K` (exit = K·floor) | 1.5 | _TBD_ | hysteresis below enter |
| `AUTOBOT_STT_RMS` (hard override) | unset | — | if set, disables adaptation (rollback) |

> Provisional defaults are conservative starting points. Replace the "Final" column with values traceable to
> the recorded RMS distribution, then update the `audio_sink.py` defaults + this note in the same change.
