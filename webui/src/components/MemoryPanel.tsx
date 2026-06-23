import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

type Fact = { text: string; kind: string; ts: number; source: string };
type Sighting = { ts: number; label: string; kind: string; detail?: string };
type MemoryData = { facts: Fact[]; sightings: Sighting[]; recent: string[]; embeddings: boolean };

const ago = (ts: number) => {
  const m = Math.max(0, (Date.now() / 1000 - ts) / 60);
  if (m < 1) return "just now";
  if (m < 60) return `${Math.round(m)}m ago`;
  if (m < 1440) return `${Math.round(m / 60)}h ago`;
  return `${Math.round(m / 1440)}d ago`;
};

export default function MemoryPanel() {
  const [data, setData] = useState<MemoryData | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    const d = await api.memory();
    if (d?.ok) setData(d);
  }, []);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [load]);

  const summarize = async () => {
    setBusy(true);
    setMsg("Distilling long-term memory with the heavy model…");
    const r = await api.summarizeMemory();
    setMsg(r?.ok ? `Cleaned ${r.before ?? "?"} → ${r.after ?? "?"} facts.` : `Summarize failed: ${r?.error ?? "?"}`);
    setBusy(false);
    load();
  };

  const clearAll = async () => {
    if (!window.confirm("Wipe ALL memory (facts, daily notes, sightings)? Enrolled faces are kept.")) return;
    setBusy(true);
    await api.clearMemory();
    setMsg("Memory wiped.");
    setBusy(false);
    load();
  };

  const forget = async (text: string) => {
    await api.forgetMemory(text);
    load();
  };

  if (!data) return <div className="text-mut hud-mono text-sm">▸ loading memory…</div>;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow">Memory</span>
        <span className="text-[11px] hud-mono text-mut">
          {data.facts.length} facts · recall: {data.embeddings ? "semantic" : "keyword"}
        </span>
        <button onClick={summarize} disabled={busy} className="ml-auto text-[11px] hud-mono bg-card2/60 border border-line rounded-lg px-3 py-1.5 hover:border-accent/50 active:scale-95 disabled:opacity-50">
          ⟳ Distill
        </button>
        <button onClick={clearAll} disabled={busy} className="text-[11px] hud-mono bg-card2/60 border border-bad/40 text-bad rounded-lg px-3 py-1.5 hover:border-bad active:scale-95 disabled:opacity-50">
          ⌫ Wipe
        </button>
      </div>
      {msg && <div className="text-[11px] hud-mono text-mut">{msg}</div>}

      <section className="hud-panel p-4">
        <div className="text-[10px] uppercase tracking-[0.2em] text-mut mb-2">Long-term facts</div>
        {data.facts.length === 0 ? (
          <div className="text-mut hud-mono text-sm">Nothing remembered yet.</div>
        ) : (
          <div className="flex flex-col gap-1.5 max-h-[40vh] overflow-y-auto">
            {data.facts.map((f, i) => (
              <div key={i} className="flex items-start gap-2 text-sm group">
                <span className="text-[10px] hud-mono text-accent/80 uppercase mt-0.5 w-[72px] shrink-0">{f.kind}</span>
                <span className="flex-1">{f.text}</span>
                <span className="text-[10px] hud-mono text-mut shrink-0">{ago(f.ts)}</span>
                <button onClick={() => forget(f.text)} title="Forget" className="text-mut hover:text-bad opacity-0 group-hover:opacity-100 shrink-0">✕</button>
              </div>
            ))}
          </div>
        )}
      </section>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <section className="hud-panel p-4">
          <div className="text-[10px] uppercase tracking-[0.2em] text-mut mb-2">Recently seen</div>
          {data.sightings.length === 0 ? (
            <div className="text-mut hud-mono text-sm">No sightings logged.</div>
          ) : (
            <div className="flex flex-col gap-1 max-h-[30vh] overflow-y-auto text-sm">
              {data.sightings.map((s, i) => (
                <div key={i} className="flex items-center gap-2">
                  <span className="text-[10px] hud-mono text-gold uppercase w-[56px] shrink-0">{s.kind}</span>
                  <span className="flex-1">{s.label}{s.detail ? ` — ${s.detail}` : ""}</span>
                  <span className="text-[10px] hud-mono text-mut shrink-0">{ago(s.ts)}</span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="hud-panel p-4">
          <div className="text-[10px] uppercase tracking-[0.2em] text-mut mb-2">Recent notes</div>
          {data.recent.length === 0 ? (
            <div className="text-mut hud-mono text-sm">No recent activity.</div>
          ) : (
            <div className="flex flex-col gap-1 max-h-[30vh] overflow-y-auto text-sm hud-mono text-mut">
              {data.recent.map((r, i) => (
                <div key={i}>· {r}</div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
