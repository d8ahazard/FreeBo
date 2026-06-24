import { useEffect, useRef, useState } from "react";
import { api } from "../api";

// Temporary Phase-0 audio-calibration tab. You pick a window, hit Start (begins a measurement epoch + the
// robot listens), READ THE SCRIPT, then hit Stop (captures + saves the RMS/STT distribution to
// data/test-evidence/audio_calibration.json). Repeat per window; the numbers set the final VAD constants.

type Script = { label: string; title: string; text: string; tts?: boolean };

const SCRIPTS: Script[] = [
  { label: "silence", title: "1 · Silence (room floor)", text: "Say NOTHING. Stay quiet for ~30s so we measure the room noise floor." },
  { label: "normal_1m", title: "2 · Normal speech @ ~1 m", text: "FreeBo, stop. Turn left. What do you see? Come here. Be quiet. Go to the kitchen. (now count slowly: one… two… three… four… five… six… seven… eight… nine… ten.)" },
  { label: "quiet", title: "3 · Quiet speech", text: "(softly) FreeBo, stop. Turn right. Come here. Be quiet. What do you see? (count one to ten quietly.)" },
  { label: "loud", title: "4 · Loud speech", text: "(loudly / projecting) FreeBo, STOP! Turn around! What do you SEE?! Come HERE! (count one to ten loudly.)" },
  { label: "room_noise", title: "5 · Speech + room noise", text: "(with TV/fan/music on) FreeBo, stop. What do you see? Come here. Turn left. (count one to ten.)" },
  { label: "tts_playback", title: "6 · TTS playback (no human speech)", text: "Click 'Make robot talk', then STAY SILENT. This measures the robot's own voice bleeding into its mic.", tts: true },
];

export default function AudioCalibratePanel() {
  const [idx, setIdx] = useState(0);
  const [running, setRunning] = useState(false);
  const [live, setLive] = useState<any>(null);
  const [result, setResult] = useState<any>(null);
  const timer = useRef<number | undefined>(undefined);
  const cur = SCRIPTS[idx];

  useEffect(() => {
    if (!running) {
      if (timer.current) window.clearInterval(timer.current);
      return;
    }
    const tick = async () => {
      try {
        const d = await api.audioDiag();
        setLive(d.audio_sink);
      } catch { /* ignore */ }
    };
    tick();
    timer.current = window.setInterval(tick, 1000);
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [running]);

  const start = async () => {
    setResult(null);
    await api.audioReset();
    setRunning(true);
  };
  const stop = async () => {
    setRunning(false);
    const r = await api.audioCapture(cur.label);
    setResult(r);
  };
  const robotTalk = () =>
    api.say("Hello, I am FreeBo. I am reading this sentence aloud so we can measure how my own voice sounds in my microphone. One, two, three, four, five, six, seven, eight, nine, ten.");

  const rms = result?.window?.rms;
  const stt = result?.window?.stt_ms;

  return (
    <section className="hud-panel p-4">
      <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow mb-3">
        ◈ Audio Calibration <span className="text-mut normal-case tracking-normal">(temp · Phase 0)</span>
      </div>

      {/* window picker */}
      <div className="flex flex-wrap gap-1.5 mb-3">
        {SCRIPTS.map((s, i) => (
          <button
            key={s.label}
            disabled={running}
            onClick={() => { setIdx(i); setResult(null); }}
            className={`text-[11px] rounded-md px-2 py-1 border transition ${
              i === idx ? "border-accent text-accent bg-accent/10" : "border-line bg-card2/50 text-mut"
            } ${running ? "opacity-50" : ""}`}
          >
            {s.label}
          </button>
        ))}
      </div>

      {/* script to read */}
      <div className="rounded-lg border border-line bg-bg/50 p-3 mb-3">
        <div className="text-xs text-accent mb-1">{cur.title}</div>
        <div className="text-sm leading-relaxed">{cur.text}</div>
        {cur.tts && (
          <button onClick={robotTalk} className="mt-2 text-[11px] bg-card2 border border-line rounded-lg px-3 py-1.5 hover:border-accent/50 active:scale-95">
            🔊 Make robot talk
          </button>
        )}
      </div>

      {/* controls */}
      <div className="flex items-center gap-2 mb-3">
        {!running ? (
          <button onClick={start} className="bg-ok/20 border border-ok text-fg rounded-lg px-4 py-2 text-sm active:scale-95">
            ▶ Start calibration
          </button>
        ) : (
          <button onClick={stop} className="bg-bad/20 border border-bad text-fg rounded-lg px-4 py-2 text-sm active:scale-95">
            ■ Stop calibration
          </button>
        )}
        {running && (
          <span className="text-[11px] hud-mono text-accent flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full bg-accent reactor inline-block" />
            LISTENING · recv {live?.recv ?? "…"} · maxRMS {live?.max_rms ?? "…"} · vad {live?.vad_starts ?? 0}
            {live?.stt_device ? ` · ${live.stt_device}` : ""}
          </span>
        )}
      </div>

      {/* last captured window */}
      {result?.window && (
        <div className="rounded-lg border border-line bg-card2/40 p-3 text-[11px] hud-mono">
          <div className="text-accent mb-1">captured “{result.label}” → {result.saved}</div>
          {rms && (
            <div>RMS n={rms.count} min={rms.min} mean={rms.mean} p50={rms.p50} p90={rms.p90} p95={rms.p95} p99={rms.p99} max={rms.max}</div>
          )}
          <div>floor={result.window.noise_floor} enter={result.window.enter_thr} exit={result.window.exit_thr} adaptive={String(result.window.adaptive)}</div>
          <div>vad_starts={result.window.vad_starts} vad_ends={result.window.vad_ends} accepted={result.window.seg_accepted} dropped={result.window.seg_dropped} drop_speaking={result.window.drop_speaking}</div>
          {stt && stt.count > 0 && <div>stt_ms n={stt.count} p50={stt.p50} p95={stt.p95} max={stt.max}</div>}
          {result.window.transcripts?.length > 0 && (
            <div className="mt-1 text-mut">transcripts: {result.window.transcripts.map((t: string) => `“${t}”`).join(" ")}</div>
          )}
        </div>
      )}
    </section>
  );
}
