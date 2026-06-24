import type { AudioStatus } from "../types";

/**
 * MicIndicator — a permanent header readout of what the robot is actually doing with the mic (operational,
 * not the Calibrate tab). The `Hear` ability is only the PERMISSION; this shows live reality:
 *   gray=off · red=no-stream/error · green=listening · yellow=hearing · blue=transcribing · purple=speaking.
 */
const STATE_STYLE: Record<string, { dot: string; text: string; pulse?: boolean }> = {
  "OFF": { dot: "bg-mut", text: "text-mut" },
  "NO MIC STREAM": { dot: "bg-bad", text: "text-bad" },
  "ERROR": { dot: "bg-bad", text: "text-bad", pulse: true },
  "LISTENING": { dot: "bg-accent", text: "text-accent" },
  "HEARING SPEECH": { dot: "bg-gold", text: "text-gold", pulse: true },
  "TRANSCRIBING": { dot: "bg-sky-400", text: "text-sky-400", pulse: true },
  "HEARD": { dot: "bg-sky-400", text: "text-sky-400" },
  "SPEAKING-ECHO-GATED": { dot: "bg-purple-400", text: "text-purple-400", pulse: true },
};

export default function MicIndicator({ audio, allowAudioIn }: { audio: AudioStatus | null; allowAudioIn: boolean }) {
  const state = !allowAudioIn ? "OFF" : audio?.state || "OFF";
  const st = STATE_STYLE[state] ?? STATE_STYLE.OFF;

  // Live input meter: current RMS relative to the enter threshold (1.0 == at threshold). Clamp to the bar.
  const enter = audio?.enter_threshold || 1;
  const level = audio ? Math.min(100, (audio.current_rms / (enter * 1.8)) * 100) : 0;
  const recentTranscript = audio?.last_transcript && audio.last_transcript_ts
    && Date.now() / 1000 - audio.last_transcript_ts < 6
    ? audio.last_transcript : "";

  const title = audio
    ? `mic: ${state}\nrms ${Math.round(audio.current_rms)} · floor ${Math.round(audio.noise_floor)} · enter ${Math.round(audio.enter_threshold)} / exit ${Math.round(audio.exit_threshold)}`
      + `\nstream ${audio.stream_live ? "live" : "down"}${audio.last_audio_age_ms != null ? ` (${audio.last_audio_age_ms}ms)` : ""}`
      + `\nqueue ${audio.stt_queue_depth}${audio.bargein_ready ? " · barge-in ready" : ""}${audio.error ? `\nerror: ${audio.error}` : ""}`
    : "mic status unavailable";

  return (
    <div className="flex items-center gap-1.5 text-[11px] hud-mono" title={title}>
      <span className={`w-2 h-2 rounded-full ${st.dot} ${st.pulse ? "animate-pulse" : ""}`} />
      <span className={`hidden md:inline ${st.text} uppercase tracking-wider`}>{state}</span>
      {/* tiny live level meter (only meaningful while a stream exists) */}
      <span className="hidden md:inline-block w-10 h-1.5 rounded bg-card2 overflow-hidden border border-line">
        <span className={`block h-full ${level >= 100 ? "bg-gold" : "bg-accent/70"}`} style={{ width: `${level}%` }} />
      </span>
      {recentTranscript && (
        <span className="hidden lg:inline text-mut max-w-[140px] truncate">“{recentTranscript}”</span>
      )}
    </div>
  );
}
