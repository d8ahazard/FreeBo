import type { Telemetry } from "../types";

/** Battery as an arc-reactor ring. */
function BatteryRing({ pct, charging }: { pct: number; charging: boolean }) {
  const r = 30;
  const c = 2 * Math.PI * r;
  const frac = Math.max(0, Math.min(100, pct)) / 100;
  const color = pct < 0 ? "var(--color-mut)" : pct <= 20 ? "var(--color-bad)" : pct <= 40 ? "var(--color-warn)" : "var(--color-accent)";
  return (
    <div className="relative w-[84px] h-[84px] shrink-0">
      <svg viewBox="0 0 84 84" className="w-full h-full -rotate-90">
        <circle cx="42" cy="42" r={r} fill="none" stroke="var(--color-line)" strokeWidth="6" />
        <circle
          cx="42" cy="42" r={r} fill="none" stroke={color} strokeWidth="6" strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - frac)}
          style={{ filter: `drop-shadow(0 0 6px ${color})`, transition: "stroke-dashoffset .5s" }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div className="text-lg font-bold hud-mono text-glow">{pct < 0 ? "—" : pct}</div>
        <div className="text-[9px] text-mut -mt-1">{charging ? "⚡ CHG" : "BATT %"}</div>
      </div>
    </div>
  );
}

function Stat({ label, value, tone = "fg" }: { label: string; value: string; tone?: "fg" | "ok" | "warn" | "bad" | "mut" }) {
  const tc = { fg: "text-fg", ok: "text-ok", warn: "text-warn", bad: "text-bad", mut: "text-mut" }[tone];
  return (
    <div className="flex items-center justify-between text-xs border-b border-line/50 py-1">
      <span className="text-[10px] uppercase tracking-wider text-mut">{label}</span>
      <span className={`hud-mono ${tc}`}>{value}</span>
    </div>
  );
}

export default function HudTelemetry({ t }: { t: Telemetry }) {
  const pct = typeof t.battery === "number" ? t.battery : -1;
  const charging = t.charge === 1;
  const tof = typeof t.tof === "number" ? t.tof : typeof t.distance === "number" ? t.distance : undefined;
  const laser = tof === undefined ? "no signal" : `${tof.toFixed(2)} m`;

  const imu = t.imu;
  let accelMag: number | undefined;
  if (imu) {
    const v = Array.isArray(imu) ? imu : [imu.ax ?? imu.x ?? 0, imu.ay ?? imu.y ?? 0, imu.az ?? imu.z ?? 0];
    accelMag = Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
  }

  return (
    <div className="hud-panel p-3">
      <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow mb-2">Telemetry</div>
      <div className="flex gap-3 items-center mb-2">
        <BatteryRing pct={pct} charging={charging} />
        <div className="flex-1 min-w-0">
          <Stat label="Link" value={t.connected ? "ONLINE" : "OFFLINE"} tone={t.connected ? "ok" : "bad"} />
          <Stat label="State" value={t.resting ? "RESTING" : t.sleeping ? "ASLEEP" : t.awake === false ? "DOZING" : "ACTIVE"} tone={t.resting || t.sleeping ? "warn" : "ok"} />
          <Stat label="Eyes" value={String(t.eyes_animation ?? "—")} tone="mut" />
        </div>
      </div>
      <Stat label="Laser (IR)" value={t.laser === undefined ? (tof === undefined ? "—" : laser) : t.laser ? "ON" : "off"} tone={t.laser ? "ok" : "mut"} />
      <Stat label="Avoid" value={t.avoidobstacle === undefined ? "—" : t.avoidobstacle ? "ON" : "off"} tone={t.avoidobstacle ? "ok" : "warn"} />
      <Stat label="Move speed" value={typeof t.moveSpeed === "number" ? `${t.moveSpeed}${typeof t.moveMode === "number" ? ` · m${t.moveMode}` : ""}` : "—"} tone="fg" />
      <Stat label="Low-batt thr" value={typeof t.lowBatteryPercentage === "number" ? `${t.lowBatteryPercentage}%` : "—"} tone="mut" />
      <Stat label="Touch / IMU" value={t.touched ? "✋ TOUCHED" : accelMag !== undefined ? `${accelMag.toFixed(2)} g` : "n/a (cloud)"} tone={t.touched ? "warn" : "mut"} />
      {typeof t.wifi === "number" && <Stat label="Wi-Fi" value={`${t.wifi}`} tone="mut" />}
    </div>
  );
}
