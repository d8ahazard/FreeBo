import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

type Task = {
  id: string;
  text: string;
  schedule: string;
  next_run: number | null;
  enabled: boolean;
  runs: number;
};

type When = "once" | "daily" | "every";

const fmtNext = (ts: number | null) => {
  if (!ts) return "—";
  const d = (ts * 1000 - Date.now()) / 1000;
  if (d < 0) return "due";
  if (d < 90) return `in ${Math.round(d)}s`;
  if (d < 5400) return `in ${Math.round(d / 60)}m`;
  return `in ${Math.round(d / 3600)}h`;
};

export default function TasksPanel() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [text, setText] = useState("");
  const [when, setWhen] = useState<When>("once");
  const [value, setValue] = useState("20");

  const load = useCallback(async () => {
    const r = await api.tasks();
    if (r?.ok) setTasks(r.tasks);
  }, []);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [load]);

  const add = async () => {
    const t = text.trim();
    if (!t) return;
    const body: { text: string; in_seconds?: number; daily_time?: string; every_seconds?: number } = { text: t };
    if (when === "once") body.in_seconds = Math.max(1, Number(value) || 1) * 60;
    else if (when === "daily") body.daily_time = value;
    else body.every_seconds = Math.max(1, Number(value) || 1) * 60;
    await api.addTask(body);
    setText("");
    load();
  };

  return (
    <section className="hud-panel p-3">
      <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow mb-2">✦ Scheduled Tasks</div>

      <div className="flex flex-col gap-2 mb-3">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="e.g. Patrol the kitchen and greet whoever's there"
          className="bg-bg/60 border border-line rounded-lg px-2 py-1.5 text-sm focus:border-accent outline-none"
        />
        <div className="flex gap-1.5 items-center text-xs">
          <select value={when} onChange={(e) => setWhen(e.target.value as When)} className="bg-bg/60 border border-line rounded-lg px-2 py-1.5 hud-mono">
            <option value="once">in (min)</option>
            <option value="daily">daily @</option>
            <option value="every">every (min)</option>
          </select>
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={when === "daily" ? "09:00" : "20"}
            className="w-20 bg-bg/60 border border-line rounded-lg px-2 py-1.5 hud-mono"
          />
          <button onClick={add} className="ml-auto bg-accent/20 border border-accent rounded-lg px-3 py-1.5 active:scale-95 hud-glow">
            + Add
          </button>
        </div>
      </div>

      {tasks.length === 0 ? (
        <div className="text-mut hud-mono text-xs">No scheduled tasks.</div>
      ) : (
        <div className="flex flex-col gap-1.5 max-h-[28vh] overflow-y-auto">
          {tasks.map((t) => (
            <div key={t.id} className="flex items-start gap-2 text-sm group">
              <span className="flex-1">{t.text}</span>
              <span className="text-[10px] hud-mono text-mut shrink-0">{t.schedule} · {fmtNext(t.next_run)}</span>
              <button onClick={() => api.cancelTask(t.id).then(load)} title="Cancel" className="text-mut hover:text-bad shrink-0">✕</button>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
