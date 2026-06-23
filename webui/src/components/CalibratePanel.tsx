import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

/**
 * CalibratePanel — pre-flight movement calibration. The robot does a few small test moves in open space and
 * measures how much the camera view changes, so autonomous driving uses controlled steps instead of blind
 * lunges. Autonomy ('auto') is gated until this is done (AUTOBOT_REQUIRE_CALIBRATION). The robot WILL move.
 */
type Profile = {
  forward_speed: number; forward_duration: number;
  turn_rx: number; turn_duration: number; baseline: number; move_threshold: number;
};

export default function CalibratePanel() {
  const [calibrated, setCalibrated] = useState<boolean | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [running, setRunning] = useState(false);
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    const r = await api.calibrateStatus();
    if (r?.ok) { setCalibrated(!!r.calibrated); setProfile(r.profile || null); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const run = async () => {
    if (!window.confirm("Calibrate movement now? The robot will make a few small test moves — make sure it's in open space.")) return;
    setRunning(true);
    setMsg("Calibrating — running test moves…");
    try {
      const r = await api.calibrate();
      if (r?.ok) {
        setMsg(r.moved_detected ? "Calibrated." : "Calibrated, but little motion detected — check it's in open space / not docked.");
        setProfile(r.profile || null);
        setCalibrated(true);
      } else {
        setMsg(`Failed: ${r?.error ?? "unknown"}`);
      }
    } catch (e) {
      setMsg(String(e));
    } finally {
      setRunning(false);
      load();
    }
  };

  return (
    <div className="hud-panel p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow">Movement Calibration</div>
        <span className={`text-[10px] hud-mono ${calibrated ? "text-ok" : "text-warn"}`}>
          {calibrated == null ? "…" : calibrated ? "✔ calibrated" : "! not calibrated"}
        </span>
      </div>
      <button
        onClick={run}
        disabled={running}
        className="w-full rounded-lg py-2 text-xs uppercase tracking-[0.15em] border border-accent/70 bg-accent/10 text-fg active:scale-95 disabled:opacity-50"
        title="The robot will drive briefly to measure its movement"
      >
        {running ? "calibrating…" : "▶ calibrate (drives briefly)"}
      </button>
      {msg && <div className="mt-2 text-[11px] text-mut hud-mono">{msg}</div>}
      {profile && (
        <div className="mt-2 text-[11px] hud-mono text-mut flex flex-col gap-0.5">
          <div>step: speed {profile.forward_speed} · {profile.forward_duration}s</div>
          <div>turn: rx {profile.turn_rx} · {profile.turn_duration}s</div>
        </div>
      )}
      {calibrated === false && (
        <div className="mt-2 text-[11px] text-warn">Autonomy stays paused until calibrated.</div>
      )}
    </div>
  );
}
