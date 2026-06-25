import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

/**
 * TimelinePanel — Phase 1 observability inspector (agent_next_3 §C5). A readable engineering timeline of the
 * structured event journal: category/type/source/outcome, requested-vs-effective, epoch/generation, latency,
 * STOP/RESUME incidents grouped by correlation, with expandable redacted detail and filters matching the API.
 */
type EventRow = {
  id: string; seq: number; ts_utc: string; category: string; type: string; source: string;
  requested?: string | null; effective?: string | null; reason?: string | null; outcome?: string | null;
  epoch?: number | null; generation?: number | null; correlation_id?: string | null;
  ticket_id?: number | null; latency_ms?: number | null; detail?: Record<string, unknown>;
};

const CATEGORIES = [
  "", "safety.transition", "safety.faculty_decision", "control.effect_admission", "control.transport",
  "reason.lifecycle", "reason.tool", "speech.lifecycle", "vision.lifecycle", "motion.lifecycle", "system.lifecycle",
];

function tone(ev: EventRow): string {
  const bad = ["denied", "degraded", "failed", "cancelled", "blocked", "stuck"];
  const o = (ev.outcome || ev.effective || "").toLowerCase();
  if (bad.some((b) => o.includes(b))) return "text-bad";
  if ((ev.effective || "").toLowerCase() === "inhibited") return "text-warn";
  return "text-fg";
}

export default function TimelinePanel() {
  const [rows, setRows] = useState<EventRow[]>([]);
  const [category, setCategory] = useState("");
  const [correlation, setCorrelation] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      if (correlation.trim()) {
        const r = await api.eventsTrace(correlation.trim());
        setRows(r.events || []);
      } else {
        const r = await api.events({ category, limit: 300 });
        setRows((r.events || []).slice().reverse());   // newest first
      }
      setSummary(await api.eventsSummary());
    } catch (e) {
      setErr(String(e));
    }
  }, [category, correlation]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 3000);
    return () => window.clearInterval(t);
  }, [load]);

  return (
    <section className="hud-panel p-3 max-w-[1100px]">
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow">Timeline</div>
        <select value={category} onChange={(e) => setCategory(e.target.value)}
                className="bg-card2 border border-line rounded px-2 py-1 text-xs">
          {CATEGORIES.map((c) => <option key={c} value={c}>{c || "all categories"}</option>)}
        </select>
        <input value={correlation} onChange={(e) => setCorrelation(e.target.value)}
               placeholder="correlation id (e.g. stop-gen3)"
               className="bg-card2 border border-line rounded px-2 py-1 text-xs w-[230px]" />
        <button onClick={load} className="text-xs border border-line rounded px-2 py-1 hover:border-accent/50">
          ↻ refresh
        </button>
        <a href={api.eventsExportUrl(correlation.trim() || undefined)} target="_blank" rel="noreferrer"
           className="text-xs border border-line rounded px-2 py-1 hover:border-accent/50">⤓ export</a>
        {summary && <span className="text-[11px] text-mut hud-mono ml-auto">
          {String((summary as { total?: number }).total ?? 0)} events</span>}
      </div>
      {err && <div className="text-bad text-xs hud-mono mb-2">⚠ {err}</div>}
      <div className="overflow-auto max-h-[70vh] text-xs">
        <table className="w-full hud-mono">
          <thead className="text-mut text-[10px] uppercase">
            <tr className="text-left">
              <th className="py-1 pr-2">time</th><th className="pr-2">category</th><th className="pr-2">type</th>
              <th className="pr-2">src</th><th className="pr-2">req→eff</th><th className="pr-2">e/g</th>
              <th className="pr-2">lat</th><th className="pr-2">outcome</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((ev) => (
              <>
                <tr key={ev.id} className="border-t border-line/40 cursor-pointer hover:bg-card2/40"
                    onClick={() => setOpen(open === ev.id ? null : ev.id)}>
                  <td className="py-1 pr-2 text-mut">{(ev.ts_utc || "").slice(11, 23)}</td>
                  <td className="pr-2">{ev.category.replace(".", "·")}</td>
                  <td className={`pr-2 ${tone(ev)}`}>{ev.type}</td>
                  <td className="pr-2 text-mut">{ev.source}</td>
                  <td className="pr-2">{(ev.requested ?? "—")}→<span className={tone(ev)}>{ev.effective ?? "—"}</span></td>
                  <td className="pr-2 text-mut">{ev.epoch ?? "—"}/{ev.generation ?? "—"}</td>
                  <td className="pr-2 text-mut">{ev.latency_ms != null ? `${ev.latency_ms}ms` : "—"}</td>
                  <td className={`pr-2 ${tone(ev)}`}>{ev.outcome ?? ""}</td>
                </tr>
                {open === ev.id && (
                  <tr key={ev.id + "-d"} className="bg-bg/60">
                    <td colSpan={8} className="p-2">
                      <pre className="whitespace-pre-wrap text-[11px] text-mut">{JSON.stringify(
                        { correlation_id: ev.correlation_id, ticket_id: ev.ticket_id, reason: ev.reason,
                          detail: ev.detail }, null, 2)}</pre>
                    </td>
                  </tr>
                )}
              </>
            ))}
            {rows.length === 0 && !err && (
              <tr><td colSpan={8} className="py-4 text-mut text-center">no events yet</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
