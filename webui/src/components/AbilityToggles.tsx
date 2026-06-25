import type { Capabilities, Settings } from "../types";

/**
 * AbilityToggles — the robot's autonomous capabilities. P0-R4.6: the lit toggle shows REQUESTED state (what
 * you asked for); the corner dot shows EFFECTIVE state from the kernel (green = live, amber = requested but
 * inhibited/blocked, grey = off). A toggle that's on but not effective shows WHY on hover.
 */
const TOGGLES: { key: keyof Settings; cap: string; label: string; icon: string; hint: string }[] = [
  { key: "allow_think", cap: "think", label: "Think", icon: "🧠", hint: "autonomous reasoning loop" },
  { key: "allow_motion", cap: "motion", label: "Move", icon: "🛞", hint: "AI-driven movement" },
  { key: "allow_video", cap: "ai_vision", label: "See", icon: "👁", hint: "feed camera to the brain" },
  { key: "allow_audio_in", cap: "listen", label: "Hear", icon: "👂", hint: "listen via the robot mic" },
  { key: "talk_enabled", cap: "speak", label: "Speak", icon: "🔊", hint: "talk through the robot speaker" },
];

export default function AbilityToggles(
  { settings, save, capabilities }:
  { settings: Settings; save: (c: Partial<Settings>) => void; capabilities?: Capabilities },
) {
  return (
    <div className="hud-panel p-3">
      <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow mb-2">Abilities</div>
      <div className="grid grid-cols-5 gap-1.5">
        {TOGGLES.map(({ key, cap, label, icon, hint }) => {
          const requested = !!settings[key];
          const c = capabilities?.[cap];
          const effective = c ? c.effective : requested;        // fall back to requested if no surface yet
          // green = effective; amber = requested but blocked; grey = not requested
          const dot = effective ? "bg-ok shadow-[0_0_6px_var(--color-ok)]"
            : requested ? "bg-warn shadow-[0_0_6px_var(--color-warn)]" : "bg-line";
          const why = c && requested && !effective ? ` — BLOCKED: ${c.reason || "inhibited"}` : "";
          return (
            <button
              key={String(key)}
              onClick={() => save({ [key]: !requested } as Partial<Settings>)}
              title={`${hint} — requested ${requested ? "ON" : "off"}, effective ${effective ? "ON" : "off"}${why}`}
              className={`relative flex flex-col items-center gap-1 rounded-lg py-2 border transition active:scale-95 ${
                requested ? "border-accent/70 bg-accent/10 text-fg" : "border-line bg-card2/50 text-mut"
              }`}
            >
              <span className={`text-base ${requested ? (effective ? "" : "opacity-60") : "grayscale opacity-50"}`}>{icon}</span>
              <span className="text-[10px]">{label}</span>
              <span className={`absolute top-1 right-1 w-1.5 h-1.5 rounded-full ${dot}`} />
            </button>
          );
        })}
      </div>
    </div>
  );
}
