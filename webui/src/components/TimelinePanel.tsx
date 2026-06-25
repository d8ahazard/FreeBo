import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { EventRow } from "../types";

/**
 * TimelinePanel — Phase 1 LIVE incident inspector (agent_next_4 §6).
 *
 * Live mode renders straight from the hook's bounded `journalEvents` buffer (no polling). A one-shot,
 * AbortController-serialized catch-up fetch widens the pool with older/persistent rows when filters change.
 * "Pause live" freezes the current view for inspection while the hook keeps buffering underneath.
 */

type Health = {
  writer_alive?: boolean;
  queue_depth?: number;
  queue_capacity?: number;
  enqueued?: number;
  persisted?: number;
  queue_dropped?: number;
  persist_failed?: number;
  recovered?: number;
  retained_files?: number;
  oldest_ts?: string | null;
  newest_ts?: string | null;
  process_session_id?: string;
};

type Incident = {
  incident_id: string;
  start_ts?: string | null;
  end_ts?: string | null;
  count?: number;
  outcome?: string | null;
  severity?: string | null;
};

const CATEGORIES = [
  "", "safety.transition", "safety.faculty_decision", "control.effect_admission", "control.transport",
  "reason.lifecycle", "reason.tool", "speech.lifecycle", "vision.lifecycle", "motion.lifecycle", "system.lifecycle",
];

const DISPLAY_CAP = 600;
const BAD_WORDS = ["denied", "degraded", "failed", "cancelled", "canceled", "blocked", "stuck", "timed_out", "timeout", "discarded", "dropped", "relatch"];

function tone(ev: EventRow): string {
  const o = `${ev.outcome ?? ""} ${ev.effective ?? ""} ${ev.type ?? ""}`.toLowerCase();
  if (BAD_WORDS.some((b) => o.includes(b))) return "text-bad";
  if (o.includes("inhibit")) return "text-warn";
  return "text-fg";
}

function sevTone(sev?: string | null): string {
  const s = (sev || "").toLowerCase();
  if (s === "critical" || s === "error") return "text-bad";
  if (s === "warn" || s === "warning") return "text-warn";
  return "text-fg";
}

function fmtTs(iso: string | undefined, utc: boolean): string {
  if (!iso) return "—";
  if (utc) return `${iso.slice(11, 23)}Z`;
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleTimeString(undefined, { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

// datetime-local ("2026-06-25T16:31") interpreted as local time -> UTC ISO for the API.
function localToIso(v: string): string {
  if (!v) return "";
  const d = new Date(v);
  return isNaN(d.getTime()) ? "" : d.toISOString();
}

function copy(text?: string | null) {
  if (text) navigator.clipboard?.writeText(text).catch(() => {});
}

export default function TimelinePanel({ journalEvents, connected }: { journalEvents: EventRow[]; connected: boolean }) {
  // filters
  const [category, setCategory] = useState("");
  const [typeF, setTypeF] = useState("");
  const [outcomeF, setOutcomeF] = useState("");
  const [sourceF, setSourceF] = useState("");
  const [incidentF, setIncidentF] = useState("");
  const [corrF, setCorrF] = useState("");
  const [startLocal, setStartLocal] = useState("");
  const [endLocal, setEndLocal] = useState("");

  // view state
  const [utc, setUtc] = useState(true);
  const [paused, setPaused] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [showTransport, setShowTransport] = useState(false);

  // server data
  const [fetched, setFetched] = useState<EventRow[]>([]);
  const [frozen, setFrozen] = useState<EventRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [selIncident, setSelIncident] = useState<string | null>(null);
  const [incidentRows, setIncidentRows] = useState<EventRow[]>([]);

  const acRef = useRef<AbortController | null>(null);

  // ── one-shot, serialized catch-up fetch (older/persistent rows) ──────────────────────────────────
  const startIso = localToIso(startLocal);
  const endIso = localToIso(endLocal);
  const loadCatchup = useCallback(async () => {
    acRef.current?.abort();
    const ac = new AbortController();
    acRef.current = ac;
    const params: Record<string, string | number> = { order: "desc", limit: 500 };
    if (category) params.category = category;
    if (typeF.trim()) params.type = typeF.trim();
    if (outcomeF.trim()) params.outcome = outcomeF.trim();
    if (sourceF.trim()) params.source = sourceF.trim();
    if (incidentF.trim()) params.incident_id = incidentF.trim();
    if (corrF.trim()) params.correlation_id = corrF.trim();
    if (startIso) { params.start = startIso; params.persistent = 1; }
    if (endIso) { params.end = endIso; params.persistent = 1; }
    setLoading(true);
    try {
      const r = await api.events(params, ac.signal);
      setFetched(r.events || []);
      setErr(null);
    } catch (e) {
      if ((e as { name?: string }).name !== "AbortError") setErr(String(e));
    } finally {
      if (acRef.current === ac) setLoading(false);
    }
  }, [category, typeF, outcomeF, sourceF, incidentF, corrF, startIso, endIso]);

  // refetch on filter change (debounced so rapid typing doesn't race; AbortController serializes)
  useEffect(() => {
    const t = window.setTimeout(loadCatchup, 300);
    return () => window.clearTimeout(t);
  }, [loadCatchup]);

  // ── journal health (poll every 5s) ──────────────────────────────────────────────────────────────
  useEffect(() => {
    let alive = true;
    const tick = () => api.eventsHealth().then((h) => { if (alive) setHealth(h); }).catch(() => {});
    tick();
    const t = window.setInterval(tick, 5000);
    return () => { alive = false; window.clearInterval(t); };
  }, []);

  // ── incidents list ──────────────────────────────────────────────────────────────────────────────
  const loadIncidents = useCallback(() => {
    api.eventsIncidents(30).then((r) => setIncidents(r.incidents || [])).catch(() => {});
  }, []);
  useEffect(() => {
    loadIncidents();
    const t = window.setInterval(loadIncidents, 10000);
    return () => window.clearInterval(t);
  }, [loadIncidents]);

  const openIncident = useCallback((id: string) => {
    setSelIncident(id);
    setIncidentRows([]);
    api.eventsIncident(id).then((r) => setIncidentRows(r.events || [])).catch((e) => setErr(String(e)));
  }, []);

  // ── filtering predicate (always client-side, applied to the merged pool) ─────────────────────────
  const startMs = startIso ? Date.parse(startIso) : NaN;
  const endMs = endIso ? Date.parse(endIso) : NaN;
  const matches = useCallback((ev: EventRow): boolean => {
    if (category && ev.category !== category) return false;
    if (typeF.trim() && !(ev.type || "").toLowerCase().includes(typeF.trim().toLowerCase())) return false;
    if (outcomeF.trim() && !(ev.outcome || "").toLowerCase().includes(outcomeF.trim().toLowerCase())) return false;
    if (sourceF.trim() && !(ev.source || "").toLowerCase().includes(sourceF.trim().toLowerCase())) return false;
    if (incidentF.trim() && ev.incident_id !== incidentF.trim()) return false;
    if (corrF.trim() && ev.correlation_id !== corrF.trim()) return false;
    if (!isNaN(startMs) && Date.parse(ev.ts_utc) < startMs) return false;
    if (!isNaN(endMs) && Date.parse(ev.ts_utc) > endMs) return false;
    return true;
  }, [category, typeF, outcomeF, sourceF, incidentF, corrF, startMs, endMs]);

  // merged pool = live buffer + catch-up fetch, deduped by id, filtered, newest-first
  const displayed = useMemo(() => {
    const byId = new Map<string, EventRow>();
    for (const ev of fetched) byId.set(ev.id, ev);
    for (const ev of journalEvents) byId.set(ev.id, ev);
    const out: EventRow[] = [];
    for (const ev of byId.values()) if (matches(ev)) out.push(ev);
    out.sort((a, b) => b.seq - a.seq);
    return out.slice(0, DISPLAY_CAP);
  }, [fetched, journalEvents, matches]);

  const viewRows = paused && frozen ? frozen : displayed;

  const togglePause = () => {
    setPaused((p) => {
      const next = !p;
      setFrozen(next ? displayed : null);
      return next;
    });
  };

  const transportRows = useMemo(
    () => viewRows.filter((ev) => ev.category === "control.transport"),
    [viewRows]
  );

  const healthBad = !!health && (!health.writer_alive || (health.persist_failed ?? 0) > 0 || (health.queue_dropped ?? 0) > 0);

  return (
    <section className="hud-panel p-3 max-w-[1280px]">
      {/* ── header: title, live/health/pause/utc ── */}
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow">Incident Inspector</div>
        <span className={`text-[10px] hud-mono px-2 py-0.5 rounded border ${connected ? "border-ok/50 text-ok" : "border-mut/50 text-mut"}`}>
          {connected ? "● live" : "○ offline"}
        </span>
        <button onClick={togglePause}
                className={`text-xs border rounded px-2 py-1 ${paused ? "border-warn text-warn" : "border-line hover:border-accent/50"}`}>
          {paused ? "▶ resume live" : "⏸ pause live"}
        </button>
        <button onClick={() => setUtc((u) => !u)} className="text-xs border border-line rounded px-2 py-1 hover:border-accent/50">
          🕑 {utc ? "UTC" : "local"}
        </button>
        {loading && <span className="text-[10px] text-mut hud-mono">loading…</span>}

        {/* journal health */}
        <div className={`ml-auto text-[10px] hud-mono px-2 py-1 rounded border ${healthBad ? "border-bad text-bad" : "border-line text-mut"}`}>
          {health ? (
            <span title={`session ${health.process_session_id ?? "?"}`}>
              writer {health.writer_alive ? "ok" : "DOWN"} · q {health.queue_depth ?? 0}/{health.queue_capacity ?? 0}
              {" · "}persisted {health.persisted ?? 0}
              {(health.queue_dropped ?? 0) > 0 ? ` · dropped ${health.queue_dropped}` : ""}
              {(health.persist_failed ?? 0) > 0 ? ` · failed ${health.persist_failed}` : ""}
              {" · files "}{health.retained_files ?? 0}
            </span>
          ) : "journal health…"}
        </div>
      </div>

      {/* ── filters ── */}
      <div className="flex items-center gap-2 mb-2 flex-wrap text-xs">
        <select value={category} onChange={(e) => setCategory(e.target.value)}
                className="bg-card2 border border-line rounded px-2 py-1">
          {CATEGORIES.map((c) => <option key={c} value={c}>{c || "all categories"}</option>)}
        </select>
        <input value={typeF} onChange={(e) => setTypeF(e.target.value)} placeholder="type"
               className="bg-card2 border border-line rounded px-2 py-1 w-[120px]" />
        <input value={outcomeF} onChange={(e) => setOutcomeF(e.target.value)} placeholder="outcome"
               className="bg-card2 border border-line rounded px-2 py-1 w-[110px]" />
        <input value={sourceF} onChange={(e) => setSourceF(e.target.value)} placeholder="source"
               className="bg-card2 border border-line rounded px-2 py-1 w-[110px]" />
        <input value={incidentF} onChange={(e) => setIncidentF(e.target.value)} placeholder="incident id"
               className="bg-card2 border border-line rounded px-2 py-1 w-[160px]" />
        <input value={corrF} onChange={(e) => setCorrF(e.target.value)} placeholder="trace: correlation id (reason-genN / action id)"
               className="bg-card2 border border-line rounded px-2 py-1 w-[260px]" />
      </div>
      <div className="flex items-center gap-2 mb-3 flex-wrap text-xs">
        <label className="text-mut">start</label>
        <input type="datetime-local" value={startLocal} onChange={(e) => setStartLocal(e.target.value)}
               className="bg-card2 border border-line rounded px-2 py-1" />
        <label className="text-mut">end</label>
        <input type="datetime-local" value={endLocal} onChange={(e) => setEndLocal(e.target.value)}
               className="bg-card2 border border-line rounded px-2 py-1" />
        <span className="text-[10px] text-mut">(time range queries persistent history)</span>
        <button onClick={loadCatchup} className="text-xs border border-line rounded px-2 py-1 hover:border-accent/50">↻ reload</button>
        <button onClick={() => { setCategory(""); setTypeF(""); setOutcomeF(""); setSourceF(""); setIncidentF(""); setCorrF(""); setStartLocal(""); setEndLocal(""); }}
                className="text-xs border border-line rounded px-2 py-1 hover:border-accent/50">✕ clear</button>
        <button onClick={() => setShowTransport((s) => !s)}
                className={`text-xs border rounded px-2 py-1 ${showTransport ? "border-accent text-accent" : "border-line hover:border-accent/50"}`}>
          ⚡ transport latency
        </button>
        <a href={api.eventsExportUrl({ correlationId: corrF.trim() || undefined, incidentId: incidentF.trim() || undefined })}
           className="text-xs border border-line rounded px-2 py-1 hover:border-accent/50">⤓ export current</a>
      </div>

      {err && <div className="text-bad text-xs hud-mono mb-2">⚠ {err}</div>}

      <div className="grid grid-cols-1 xl:grid-cols-[260px_1fr] gap-3">
        {/* ── incidents column ── */}
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <div className="text-[10px] uppercase tracking-[0.2em] text-mut">Incidents</div>
            <button onClick={loadIncidents} className="text-[10px] border border-line rounded px-1.5 py-0.5 hover:border-accent/50">↻</button>
          </div>
          <div className="overflow-auto max-h-[30vh] flex flex-col gap-1">
            {incidents.length === 0 && <div className="text-mut text-[11px]">no incidents</div>}
            {incidents.map((inc) => (
              <button key={inc.incident_id} onClick={() => openIncident(inc.incident_id)}
                      className={`text-left text-[11px] hud-mono border rounded px-2 py-1 hover:border-accent/50 ${selIncident === inc.incident_id ? "border-accent bg-accent/10" : "border-line bg-card2/40"}`}>
                <div className={`truncate ${sevTone(inc.severity)}`}>{inc.severity || "info"} · {inc.outcome || "—"}</div>
                <div className="text-mut truncate">{fmtTs(inc.start_ts ?? undefined, utc)} → {fmtTs(inc.end_ts ?? undefined, utc)} · {inc.count ?? 0}</div>
                <div className="text-mut truncate">{inc.incident_id}</div>
              </button>
            ))}
          </div>

          {selIncident && (
            <div className="mt-1 border-t border-line/60 pt-2">
              <div className="flex items-center gap-1 mb-1">
                <div className="text-[10px] uppercase tracking-[0.2em] text-accent flex-1 truncate">Trace</div>
                <button onClick={() => copy(selIncident)} className="text-[10px] border border-line rounded px-1.5 py-0.5 hover:border-accent/50">copy id</button>
                <a href={api.eventsExportUrl({ incidentId: selIncident })} className="text-[10px] border border-line rounded px-1.5 py-0.5 hover:border-accent/50">⤓</a>
                <button onClick={() => { setSelIncident(null); setIncidentRows([]); }} className="text-[10px] border border-line rounded px-1.5 py-0.5 hover:border-accent/50">✕</button>
              </div>
              <div className="overflow-auto max-h-[34vh] text-[11px] hud-mono flex flex-col gap-0.5">
                {incidentRows.length === 0 && <div className="text-mut">loading trace…</div>}
                {incidentRows.map((ev) => (
                  <div key={ev.id} className="border-b border-line/30 pb-0.5">
                    <span className="text-mut">{fmtTs(ev.ts_utc, utc)} </span>
                    <span className="text-mut">{ev.category.replace("safety.", "").replace("control.", "").replace(".lifecycle", "")}·</span>
                    <span className={tone(ev)}>{ev.type}</span>
                    {ev.phase ? <span className="text-mut"> [{ev.phase}]</span> : null}
                    {ev.latency_ms != null ? <span className="text-mut"> {ev.latency_ms}ms</span> : null}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* ── main table / transport view ── */}
        <div className="min-w-0">
          {showTransport ? (
            <div className="overflow-auto max-h-[70vh] text-xs">
              <div className="text-[10px] uppercase tracking-[0.2em] text-mut mb-2">control.transport stages</div>
              <table className="w-full hud-mono">
                <thead className="text-mut text-[10px] uppercase">
                  <tr className="text-left">
                    <th className="py-1 pr-2">time</th><th className="pr-2">stage</th><th className="pr-2">cmd</th>
                    <th className="pr-2">tkt</th><th className="pr-2">e/g</th><th className="pr-2">latency</th><th className="pr-2">outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {transportRows.map((ev) => (
                    <tr key={ev.id} className="border-t border-line/40">
                      <td className="py-1 pr-2 text-mut">{fmtTs(ev.ts_utc, utc)}</td>
                      <td className={`pr-2 ${tone(ev)}`}>{ev.type}</td>
                      <td className="pr-2 text-mut truncate max-w-[120px]">{ev.command_id ?? "—"}</td>
                      <td className="pr-2 text-mut">{ev.ticket_id ?? "—"}</td>
                      <td className="pr-2 text-mut">{ev.epoch ?? "—"}/{ev.generation ?? "—"}</td>
                      <td className="pr-2">{ev.latency_ms != null ? `${ev.latency_ms}ms` : "—"}</td>
                      <td className={`pr-2 ${tone(ev)}`}>{ev.outcome ?? ""}</td>
                    </tr>
                  ))}
                  {transportRows.length === 0 && <tr><td colSpan={7} className="py-4 text-mut text-center">no transport events in view</td></tr>}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="overflow-auto max-h-[70vh] text-xs">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-[10px] text-mut hud-mono">{viewRows.length} shown{paused ? " · paused" : ""}</span>
              </div>
              <table className="w-full hud-mono">
                <thead className="text-mut text-[10px] uppercase">
                  <tr className="text-left">
                    <th className="py-1 pr-2">time</th><th className="pr-2">category</th><th className="pr-2">type</th>
                    <th className="pr-2">src</th><th className="pr-2">req→eff</th><th className="pr-2">e/g</th>
                    <th className="pr-2">tkt</th><th className="pr-2">cmd</th><th className="pr-2">lat</th><th className="pr-2">outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {viewRows.map((ev) => (
                    <Fragment key={ev.id}>
                      <tr className="border-t border-line/40 cursor-pointer hover:bg-card2/40"
                          onClick={() => setOpenId(openId === ev.id ? null : ev.id)}>
                        <td className="py-1 pr-2 text-mut whitespace-nowrap">{fmtTs(ev.ts_utc, utc)}</td>
                        <td className="pr-2 text-mut">{ev.category.replace(".", "·")}</td>
                        <td className={`pr-2 ${tone(ev)}`}>{ev.type}</td>
                        <td className="pr-2 text-mut">{ev.source}</td>
                        <td className="pr-2">{ev.requested ?? "—"}→<span className={tone(ev)}>{ev.effective ?? "—"}</span></td>
                        <td className="pr-2 text-mut">{ev.epoch ?? "—"}/{ev.generation ?? "—"}</td>
                        <td className="pr-2 text-mut">{ev.ticket_id ?? "—"}</td>
                        <td className="pr-2 text-mut truncate max-w-[110px]">{ev.command_id ?? "—"}</td>
                        <td className="pr-2 text-mut">{ev.latency_ms != null ? `${ev.latency_ms}ms` : "—"}</td>
                        <td className={`pr-2 ${tone(ev)}`}>{ev.outcome ?? ""}</td>
                      </tr>
                      {openId === ev.id && (
                        <tr className="bg-bg/60">
                          <td colSpan={10} className="p-2">
                            <div className="flex items-center gap-2 mb-2 flex-wrap text-[10px]">
                              <span className="text-mut">id <span className="text-fg">{ev.id}</span></span>
                              <button onClick={() => copy(ev.id)} className="border border-line rounded px-1.5 py-0.5 hover:border-accent/50">copy id</button>
                              {ev.correlation_id && <button onClick={() => copy(ev.correlation_id)} className="border border-line rounded px-1.5 py-0.5 hover:border-accent/50">copy corr</button>}
                              {ev.incident_id && <button onClick={() => copy(ev.incident_id)} className="border border-line rounded px-1.5 py-0.5 hover:border-accent/50">copy incident</button>}
                              {ev.correlation_id && <button onClick={() => setCorrF(ev.correlation_id!)} className="border border-line rounded px-1.5 py-0.5 hover:border-accent/50">trace corr</button>}
                              {ev.incident_id && <button onClick={() => openIncident(ev.incident_id!)} className="border border-line rounded px-1.5 py-0.5 hover:border-accent/50">open incident</button>}
                            </div>
                            <pre className="whitespace-pre-wrap text-[11px] text-mut">{JSON.stringify(
                              {
                                seq: ev.seq, phase: ev.phase, reason: ev.reason,
                                incident_id: ev.incident_id, correlation_id: ev.correlation_id,
                                parent_event_id: ev.parent_event_id, command_id: ev.command_id, ticket_id: ev.ticket_id,
                                process_session_id: ev.process_session_id, process_instance_id: ev.process_instance_id,
                                sidecar_instance_id: ev.sidecar_instance_id, ts_utc: ev.ts_utc, detail: ev.detail,
                              }, null, 2)}</pre>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                  {viewRows.length === 0 && !err && (
                    <tr><td colSpan={10} className="py-4 text-mut text-center">no events match — waiting for live journal…</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
