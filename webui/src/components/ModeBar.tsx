import { useState } from "react";
import type { Mode, Settings } from "../types";

/**
 * ModeBar — picks what FreeBo DOES with its autonomy. Explore (roam + map), Command (pursue one directive
 * like "find and follow my cat"), Conversational (hold position, only rotate to track who it's talking to).
 * Autonomy (manual/auto) is the master gate: in manual the AI never drives, even in a roaming mode.
 */
const MODES: { key: Mode; label: string; icon: string; blurb: string }[] = [
  { key: "observe", label: "Observe", icon: "👁", blurb: "Stay put, rotate to look & comment (never roams)" },
  { key: "explore", label: "Explore / Roam", icon: "🧭", blurb: "Actively roam: greet, patrol, cover new ground" },
  { key: "command", label: "Command", icon: "🎯", blurb: "Pursue one directive" },
  { key: "conversational", label: "Converse", icon: "💬", blurb: "Hold still, track who's talking" },
];

export default function ModeBar({ settings, save }: { settings: Settings; save: (c: Partial<Settings>) => void }) {
  const [directive, setDirective] = useState(settings.directive || "");

  return (
    <div className="hud-panel p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow">Mode</div>
        <div className="flex gap-1">
          {(["manual", "assist", "auto"] as const).map((a) => (
            <button
              key={a}
              onClick={() => save({ autonomy: a })}
              className={`text-[10px] uppercase tracking-wider rounded px-2 py-0.5 border transition ${
                settings.autonomy === a
                  ? "border-gold text-gold"
                  : "border-line text-mut hover:text-fg"
              }`}
              title={
                a === "manual" ? "AI cannot drive (you steer)"
                : a === "assist" ? "AI acts only when triggered (speech/step), no autonomous wandering"
                : "AI may drive + wander autonomously (clamped to safety)"
              }
            >
              {a === "manual" ? "◌ manual" : a === "assist" ? "◐ assist" : "⚡ auto"}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2">
        {MODES.map((m) => {
          const on = settings.mode === m.key;
          return (
            <button
              key={m.key}
              onClick={() => save({ mode: m.key })}
              className={`group flex flex-col items-center gap-1 rounded-lg py-2.5 border transition active:scale-95 ${
                on ? "border-accent bg-accent/10 hud-glow" : "border-line bg-card2/60 hover:border-accent/50"
              }`}
              title={m.blurb}
            >
              <span className="text-lg">{m.icon}</span>
              <span className={`text-xs ${on ? "text-fg text-glow" : "text-mut"}`}>{m.label}</span>
            </button>
          );
        })}
      </div>

      {settings.mode === "command" && (
        <div className="mt-3">
          <div className="text-[10px] uppercase tracking-wider text-mut mb-1">Directive</div>
          <div className="flex gap-2">
            <input
              value={directive}
              onChange={(e) => setDirective(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && save({ directive })}
              placeholder='e.g. "find and follow my cat"'
              className="flex-1 bg-bg/60 border border-line rounded-lg px-3 py-2 text-sm hud-mono focus:border-accent outline-none"
            />
            <button
              onClick={() => save({ directive })}
              className="bg-accent/20 border border-accent text-fg rounded-lg px-3 py-2 text-sm active:scale-95 hud-glow"
            >
              Set
            </button>
          </div>
          {settings.directive && (
            <div className="text-[11px] text-accent mt-1.5 hud-mono">▸ active: {settings.directive}</div>
          )}
        </div>
      )}
      <div className="text-[11px] text-mut mt-2">{MODES.find((m) => m.key === settings.mode)?.blurb}</div>
    </div>
  );
}
