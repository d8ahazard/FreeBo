import { useCallback, useEffect, useRef, useState } from "react";
import { api, connectWs } from "../api";
import type { AudioStatus, AutobotEvent, BrainStatus, EventRow, FeedItem, Identity, OverseerLogItem, PendingApproval, Settings, Telemetry, TtsState } from "../types";

// Bounded client-side journal buffer: the /ws channel seeds a catch-up window then streams live events.
// Cap keeps browser memory bounded; dedup by event id suppresses overlap between catch-up and live.
const JOURNAL_CAP = 800;

let _feedId = 0;
let _overseerId = 0;

export function useAutobot() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [telemetry, setTelemetry] = useState<Telemetry>({});
  const [brain, setBrain] = useState<BrainStatus | null>(null);
  const [tts, setTts] = useState<TtsState | null>(null);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [overseerLog, setOverseerLog] = useState<OverseerLogItem[]>([]);
  const [connected, setConnected] = useState(false);
  const [estopLatched, setEstopLatched] = useState(false);
  const [audioStatus, setAudioStatus] = useState<AudioStatus | null>(null);
  const [journalEvents, setJournalEvents] = useState<EventRow[]>([]);
  const journalIds = useRef<Set<string>>(new Set());

  const pushJournal = useCallback((ev: EventRow) => {
    if (!ev || !ev.id || journalIds.current.has(ev.id)) return;
    journalIds.current.add(ev.id);
    setJournalEvents((arr) => {
      // keep chronological by seq; events usually arrive in order, so this stays cheap.
      let next = arr.length && arr[arr.length - 1].seq <= ev.seq ? [...arr, ev] : [...arr, ev].sort((a, b) => a.seq - b.seq);
      if (next.length > JOURNAL_CAP) {
        const dropped = next.slice(0, next.length - JOURNAL_CAP);
        for (const d of dropped) journalIds.current.delete(d.id);
        next = next.slice(next.length - JOURNAL_CAP);
      }
      return next;
    });
  }, []);

  const pushFeed = useCallback((item: Omit<FeedItem, "id">) => {
    setFeed((f) => {
      const next = [...f, { ...item, id: _feedId++ }];
      return next.length > 200 ? next.slice(-200) : next;
    });
  }, []);

  const pushOverseer = useCallback((item: Omit<OverseerLogItem, "id">) => {
    setOverseerLog((l) => {
      const next = [...l, { ...item, id: _overseerId++ }];
      return next.length > 80 ? next.slice(-80) : next;
    });
  }, []);

  const onEvent = useCallback(
    (e: AutobotEvent) => {
      setConnected(true);
      switch (e.type) {
        case "hello":
          setSettings(e.settings);
          setBrain(e.brain);
          setTts(e.tts);
          if (typeof e.brain?.estop_latched === "boolean") setEstopLatched(e.brain.estop_latched);
          if (e.audio) setAudioStatus(e.audio);
          if (e.identity) {
            setIdentity(e.identity);
            setApprovals(e.identity.pending ?? []);
          }
          break;
        case "settings":
          setSettings(e.settings);
          break;
        case "telemetry":
          setTelemetry(e.telemetry);
          break;
        case "observation":
          setTelemetry(e.telemetry);
          pushFeed({ kind: "sees", text: e.summary, ts: e.ts });
          break;
        case "thought":
          pushFeed({ kind: "thought", text: e.text, ts: e.ts });
          break;
        case "speech":
          // FreeBo speaks through the ROBOT'S speaker (server publishes the audio into the Agora call) —
          // never the browser. We only log the line here.
          pushFeed({ kind: "thought", text: "🔊 " + e.text, ts: e.ts });
          break;
        case "tool_call":
          pushFeed({ kind: "action", text: e.name, detail: JSON.stringify(e.args), ts: e.ts });
          break;
        case "tool_result":
          pushFeed({ kind: "result", text: e.name, detail: JSON.stringify(e.result), ts: e.ts });
          break;
        case "status":
          setBrain((b) => (b ? { ...b, status: e.status, error: e.error } : b));
          if (e.error) pushFeed({ kind: "error", text: e.error, ts: e.ts });
          break;
        case "error":
          pushFeed({ kind: "error", text: e.error, ts: e.ts });
          break;
        case "estop":
          setEstopLatched(true);
          setBrain((b) => (b ? { ...b, estop_latched: true, master_inhibited: true } : b));
          pushFeed({ kind: "estop", text: "MASTER STOP — all autonomy inhibited until RESUME", ts: Date.now() / 1000 });
          break;
        case "estop_reset":
          setEstopLatched(false);
          setBrain((b) => (b ? { ...b, estop_latched: false, master_inhibited: false } : b));
          pushFeed({ kind: "estop", text: "RESUMED — faculties restore to their toggles (still manual)", ts: Date.now() / 1000 });
          break;
        case "capabilities":
          // P0-R4.6 / §9: fold the authoritative snapshot into brain so toggles/readiness show effective
          // state, AND repair the local STOP display from authoritative master_inhibited (so a missed
          // estop/estop_reset event can never leave the banner wrong).
          setEstopLatched(!!e.master_inhibited);
          setBrain((b) => (b ? { ...b, capabilities: e.capabilities, master_inhibited: e.master_inhibited,
                                  estop_latched: !!e.master_inhibited,
                                  control_generation: e.generation } : b));
          break;
        case "audio_status":
          setAudioStatus(e.audio);
          break;
        case "approval_request":
          setApprovals((a) => [...a.filter((p) => p.id !== e.id), { id: e.id, tool: e.tool, args: e.args, requester: e.requester, reason: e.reason, ts: e.ts }]);
          pushFeed({ kind: "approval", text: `${e.requester} wants: ${e.tool}`, detail: e.reason, ts: e.ts });
          break;
        case "approval_resolved":
          setApprovals((a) => a.filter((p) => p.id !== e.id));
          pushFeed({ kind: "approval", text: e.approved ? "You approved a command" : "You denied a command", ts: e.ts });
          break;
        case "proposal":
          pushOverseer({ kind: "proposal", verb: e.verb, args: e.args, ts: e.ts });
          break;
        case "overseer_act":
          pushOverseer({ kind: "act", verb: e.kind, args: e.args, result: e.result, ts: e.ts });
          break;
        case "journal_event":
          pushJournal(e.event);
          break;
      }
    },
    [pushFeed, pushOverseer, pushJournal]
  );

  useEffect(() => {
    api.state().then((s) => {
      setSettings(s.settings);
      setBrain(s.brain);
      setTts(s.tts);
      if (typeof s.brain?.estop_latched === "boolean") setEstopLatched(s.brain.estop_latched);
      if (s.audio) setAudioStatus(s.audio);
      if (s.identity) {
        setIdentity(s.identity);
        setApprovals(s.identity.pending ?? []);
      }
    });
    const off = connectWs(onEvent);
    return off;
  }, [onEvent]);

  const save = useCallback(async (changes: Partial<Settings>) => {
    const res = await api.saveSettings(changes);
    if (res.settings) setSettings(res.settings);
    return res;
  }, []);

  return { settings, telemetry, brain, tts, feed, identity, approvals, overseerLog, connected, estopLatched, audioStatus, journalEvents, save, pushFeed };
}
