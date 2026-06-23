import type { OverseerLogItem, Settings } from "../types";

/**
 * OverseerPanel — the puppet-mode HUD. When Overseer is ON the AI brain is paralyzed: it keeps thinking and
 * "tries" to drive, but every robot-affecting call is intercepted (shown here as a PROPOSAL) instead of
 * reaching the robot. A human/agent overseer drives the real robot via the API; those executed commands show
 * up here as ACTs. This panel is intentionally thin — the real control surface is the /api/overseer/* API.
 */
function fmtArgs(args: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(args)) {
    if (v === undefined || v === null || v === "") continue;
    parts.push(`${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
  }
  return parts.join(" ");
}

export default function OverseerPanel({
  settings,
  save,
  log,
}: {
  settings: Settings;
  save: (c: Partial<Settings>) => void;
  log: OverseerLogItem[];
}) {
  const on = !!settings.overseer;
  const recent = [...log].slice(-12).reverse();

  return (
    <div className={`hud-panel p-3 ${on ? "border-gold/60" : ""}`}>
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-[0.2em] text-gold text-glow">Overseer</div>
        <button
          onClick={() => save({ overseer: !on })}
          title={on ? "Brain is PARALYZED — overseer drives the robot" : "Brain controls the robot normally"}
          className={`text-[11px] rounded-lg px-3 py-1 border transition active:scale-95 ${
            on ? "border-gold text-gold bg-gold/10" : "border-line text-mut bg-card2/50"
          }`}
        >
          {on ? "● PUPPET ON" : "○ off"}
        </button>
      </div>

      <div className="text-[10px] text-mut hud-mono mb-2">
        {on
          ? "Brain paralyzed — its drive/say/action calls are intercepted. Overseer drives via /api/overseer/act."
          : "Off — the AI brain drives the robot directly."}
      </div>

      <div className="flex flex-col gap-1 max-h-[220px] overflow-y-auto">
        {recent.length === 0 ? (
          <div className="text-[11px] text-mut hud-mono py-2">No intercepted intents or overseer commands yet.</div>
        ) : (
          recent.map((it) => {
            const isAct = it.kind === "act";
            const okBad = isAct && it.result && it.result.ok === false;
            return (
              <div
                key={it.id}
                className={`flex items-center gap-2 text-[11px] hud-mono rounded px-2 py-1 border ${
                  isAct ? "border-accent/40 bg-accent/5" : "border-line bg-card2/40"
                }`}
              >
                <span className={`uppercase text-[9px] tracking-wider ${isAct ? "text-accent" : "text-mut"}`}>
                  {isAct ? "ACT" : "want"}
                </span>
                <span className="text-fg">{it.verb}</span>
                <span className="text-mut truncate flex-1">{fmtArgs(it.args)}</span>
                {okBad && <span className="text-bad">✕</span>}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
