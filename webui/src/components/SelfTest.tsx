import { useState } from "react";
import { api } from "../api";

/**
 * SelfTest — runs the live capability self-test (GET /api/selftest) in-process against the running robot and
 * shows a PASS/FAIL/SKIP row per capability. Safe by default (no driving); tick "motion" to include the
 * move/rotate/autonomy checks. The backend always restores settings + e-stops afterward.
 */
type Result = { name: string; status: string; detail: string; hint?: string };

const TONE: Record<string, string> = { PASS: "text-ok", FAIL: "text-bad", WARN: "text-warn", SKIP: "text-mut" };
const SYM: Record<string, string> = { PASS: "✔", FAIL: "✘", WARN: "!", SKIP: "–" };

export default function SelfTest() {
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<Result[] | null>(null);
  const [withMotion, setWithMotion] = useState(false);
  const [err, setErr] = useState("");

  const run = async () => {
    setRunning(true);
    setErr("");
    setResults(null);
    try {
      const rep = await api.selftest({ move: withMotion });
      if (rep && rep.reachable === false) setErr(rep.error || "app unreachable");
      else setResults((rep && rep.results) || []);
    } catch (e) {
      setErr(String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="hud-panel p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow">Self-Test</div>
        <label className="text-[10px] text-mut flex items-center gap-1 cursor-pointer" title="include move/rotate/autonomy (the robot will drive briefly)">
          <input type="checkbox" checked={withMotion} onChange={(e) => setWithMotion(e.target.checked)} /> motion
        </label>
      </div>
      <button
        onClick={run}
        disabled={running}
        className="w-full rounded-lg py-2 text-xs uppercase tracking-[0.15em] border border-accent/70 bg-accent/10 text-fg active:scale-95 disabled:opacity-50"
      >
        {running ? "running…" : withMotion ? "▶ run (drives briefly)" : "▶ run self-test"}
      </button>
      {err && <div className="mt-2 text-[11px] text-bad hud-mono">{err}</div>}
      {results && (
        <div className="mt-2 flex flex-col gap-1">
          {results.length === 0 && <div className="text-[11px] text-mut hud-mono">no checks run</div>}
          {results.map((r) => (
            <div key={r.name} className="text-[11px] hud-mono flex gap-2" title={r.hint || r.detail}>
              <span className={TONE[r.status] || "text-mut"}>{SYM[r.status] || "?"}</span>
              <span className="w-16 text-mut">{r.name}</span>
              <span className="flex-1 truncate">{r.detail}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
