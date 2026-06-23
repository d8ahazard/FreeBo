import type { Telemetry } from "../types";

function Chip({ children, tone = "mut" }: { children: React.ReactNode; tone?: "mut" | "ok" | "warn" | "bad" }) {
  const tones: Record<string, string> = {
    mut: "bg-card2 text-mut border-line",
    ok: "bg-ok text-[#0c1b12] border-ok",
    warn: "bg-warn text-[#1c1707] border-warn",
    bad: "bg-bad text-white border-bad",
  };
  return (
    <span className={`text-xs font-semibold px-2.5 py-1 rounded-full border ${tones[tone]}`}>{children}</span>
  );
}

export default function TelemetryChips({ t }: { t: Telemetry }) {
  const conn = t.connected;
  const state = !conn ? ["offline", "bad"] : t.paused ? ["released", "warn"] : t.awake ? ["live", "ok"] : ["asleep", "warn"];
  const on = Object.entries(t.toggles || {})
    .filter(([, v]) => v)
    .map(([k]) => k);
  return (
    <div className="flex gap-2 items-center flex-wrap">
      <Chip tone={state[1] as "ok" | "warn" | "bad"}>{state[0]}</Chip>
      {typeof t.battery === "number" && t.battery >= 0 && (
        <Chip>{(t.charge && t.charge > 0 ? "⚡ " : "🔋 ") + t.battery + "%"}</Chip>
      )}
      {on.length > 0 && <Chip>{on.join(" · ")}</Chip>}
      {t.eyes_animation && <Chip>👀 {t.eyes_animation}</Chip>}
      {t.audio_out && t.audio_out.available === false && <Chip tone="warn">no talkback</Chip>}
      {t.codec && (
        <span className="text-[11px] text-mut font-mono ml-auto">
          RX {t.frames_received ?? 0} · {t.codec}
        </span>
      )}
    </div>
  );
}
