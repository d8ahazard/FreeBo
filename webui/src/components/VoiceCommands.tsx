/**
 * VoiceCommands — a quick reference of the spoken orders the robot always respects. It also understands
 * paraphrases (the cortex interprets natural language), so these are examples, not an exact phrase list.
 */
const CMDS: { say: string; does: string }[] = [
  { say: "Stop / hold still", does: "stop moving now" },
  { say: "Go explore / look around", does: "roam the house" },
  { say: "Come here / follow me", does: "drive over to you" },
  { say: "Go home / dock", does: "return to the charger" },
  { say: "Back up, you're stuck", does: "reverse + turn free" },
  { say: "Shut up / be quiet", does: "stop talking a while" },
  { say: "I can't hear you", does: "repeat that, louder" },
  { say: "Go to sleep", does: "go dark (wake in UI)" },
];

export default function VoiceCommands() {
  return (
    <div className="hud-panel p-3">
      <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow mb-2">Voice Commands</div>
      <div className="flex flex-col gap-1">
        {CMDS.map((c) => (
          <div key={c.say} className="flex gap-2 text-[11px] hud-mono">
            <span className="flex-1 text-fg">"{c.say}"</span>
            <span className="text-mut">{c.does}</span>
          </div>
        ))}
      </div>
      <div className="mt-2 text-[10px] text-mut">It also understands paraphrases — just talk to it.</div>
    </div>
  );
}
