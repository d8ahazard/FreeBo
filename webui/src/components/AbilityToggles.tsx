import type { Settings } from "../types";

/**
 * AbilityToggles — the robot's autonomous capabilities. The brain actually respects these: when an ability
 * is off the corresponding tools are withheld from the AI (it won't keep trying and failing). Manual control
 * (joystick/eyes) always works regardless.
 */
const TOGGLES: { key: keyof Settings; label: string; icon: string; hint: string }[] = [
  { key: "allow_think", label: "Think", icon: "🧠", hint: "autonomous reasoning loop" },
  { key: "allow_motion", label: "Move", icon: "🛞", hint: "AI-driven movement" },
  { key: "allow_video", label: "See", icon: "👁", hint: "feed camera to the brain" },
  { key: "allow_audio_in", label: "Hear", icon: "👂", hint: "listen via the robot mic" },
  { key: "talk_enabled", label: "Speak", icon: "🔊", hint: "talk through the robot speaker" },
];

export default function AbilityToggles({ settings, save }: { settings: Settings; save: (c: Partial<Settings>) => void }) {
  return (
    <div className="hud-panel p-3">
      <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow mb-2">Abilities</div>
      <div className="grid grid-cols-5 gap-1.5">
        {TOGGLES.map(({ key, label, icon, hint }) => {
          const on = !!settings[key];
          return (
            <button
              key={String(key)}
              onClick={() => save({ [key]: !on } as Partial<Settings>)}
              title={`${hint} — ${on ? "ALLOWED" : "blocked"}`}
              className={`relative flex flex-col items-center gap-1 rounded-lg py-2 border transition active:scale-95 ${
                on ? "border-accent/70 bg-accent/10 text-fg" : "border-line bg-card2/50 text-mut"
              }`}
            >
              <span className={`text-base ${on ? "" : "grayscale opacity-50"}`}>{icon}</span>
              <span className="text-[10px]">{label}</span>
              <span
                className={`absolute top-1 right-1 w-1.5 h-1.5 rounded-full ${
                  on ? "bg-accent shadow-[0_0_6px_var(--color-accent)]" : "bg-line"
                }`}
              />
            </button>
          );
        })}
      </div>
    </div>
  );
}
