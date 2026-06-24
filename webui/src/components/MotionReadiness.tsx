import type { BrainStatus, Settings, Telemetry } from "../types";

/**
 * MotionReadiness — the single place an operator reads to know whether the robot CAN move and, if not, the
 * one exact reason why (no inferring from five unrelated buttons). The headline mirrors the gates the safety
 * floor + ActionExecutor actually enforce; the chips below break it down.
 */
function Chip({ label, ok, warn, value }: { label: string; ok?: boolean; warn?: boolean; value: string }) {
  const tone = warn ? "border-warn/50 text-warn" : ok ? "border-accent/50 text-accent" : "border-bad/50 text-bad";
  return (
    <div className={`flex items-center justify-between gap-2 rounded border px-2 py-1 text-[11px] hud-mono ${tone}`}>
      <span className="text-mut uppercase tracking-wider">{label}</span>
      <span>{value}</span>
    </div>
  );
}

export default function MotionReadiness({ brain, settings, t, estopLatched }: {
  brain: BrainStatus | null;
  settings: Settings;
  t: Telemetry;
  estopLatched: boolean;
}) {
  const latched = estopLatched || !!brain?.estop_latched;
  const connected = !!t.connected;
  const act = brain?.active_action ?? null;
  const ready = !!brain?.motion_ready && !latched;

  let headline: string;
  let tone: string;
  if (latched) {
    headline = "BLOCKED: E-STOP latched";
    tone = "bg-bad/20 text-bad border-bad animate-pulse";
  } else if (act) {
    headline = `MOVING: ${act.id} ${act.kind}`;
    tone = "bg-accent/15 text-accent border-accent/60";
  } else if (ready) {
    headline = `READY: ${settings.mode}`;
    tone = "bg-accent/10 text-accent border-accent/40";
  } else {
    headline = `BLOCKED: ${brain?.motion_block_reason || "unknown"}`;
    tone = "bg-warn/15 text-warn border-warn/50";
  }

  const fmtAge = (a: number | null | undefined) => (typeof a === "number" ? `${a.toFixed(1)}s` : "—");
  const videoStale = typeof brain?.video_age === "number" && brain.video_age > (settings.video_max_age_s ?? 2);
  const telStale = typeof brain?.telemetry_age === "number" && brain.telemetry_age > (settings.telemetry_max_age_s ?? 5);

  return (
    <section className="hud-panel p-3">
      <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow mb-2">Motion readiness</div>
      <div className={`rounded-lg border px-3 py-2 text-sm font-bold mb-2 ${tone}`}>{headline}</div>
      <div className="grid grid-cols-2 gap-1.5">
        <Chip label="E-STOP" ok={!latched} value={latched ? "LATCHED" : "clear"} />
        <Chip label="RTM" ok={connected} value={connected ? "connected" : "down"} />
        <Chip label="Autonomy" ok={settings.autonomy === "auto"} warn={settings.autonomy !== "auto"} value={settings.autonomy} />
        <Chip label="Move" ok={!!settings.allow_motion} value={settings.allow_motion ? "enabled" : "off"} />
        <Chip label="Calibrated" ok={!!brain?.calibrated} warn={!brain?.calibrated} value={brain?.calibrated ? "yes" : "no"} />
        <Chip label="Breaker" ok={!brain?.hold} warn={!!brain?.hold} value={brain?.hold ? "HOLD" : "ok"} />
        <Chip label="Resting" ok={!t.resting} warn={!!t.resting} value={t.resting ? "resting" : "active"} />
        <Chip label="Behavior" ok value={`${brain?.behavior?.scope ?? "—"}/${brain?.behavior?.intent ?? "—"}`} />
        <Chip label="Video age" ok={!videoStale} warn={videoStale} value={fmtAge(brain?.video_age)} />
        <Chip label="Telem age" ok={!telStale} warn={telStale} value={fmtAge(brain?.telemetry_age)} />
      </div>
      {act && (
        <div className="text-[11px] hud-mono text-mut mt-2">
          active: <span className="text-accent">{act.id}</span> · {act.kind} · {act.state}{act.result ? ` · ${act.result}` : ""}
        </div>
      )}
    </section>
  );
}
