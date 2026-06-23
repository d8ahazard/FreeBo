import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { FeedItem, Settings, Telemetry } from "../types";
import Joystick from "./Joystick";
import { TerminatorFeed } from "./ThoughtFeed";

/**
 * Control — the single live control surface for the EBO Air 2 (cloud). One Agora video, one analog joystick
 * (manual drive over Agora RTM, always available), the robot's per-capability toggles (think/move/see/hear/
 * speak), expressive-eye buttons, a speaker test, and the brain bridge (frames -> brain, brain cmds -> RTM,
 * hands-free robot-mic STT). Manual control here bypasses the AI capability gates — it's your hands on the robot.
 */
type Session = {
  ok: boolean;
  app_id: string;
  sid: string;
  ebo_id: string;
  rtc: { channel: string; uid: number | string; token: string; robot_uid: number | string };
  rtm: { uid: string; token: string; robot_uid: string };
  error?: string;
};

const RTM_LOGIN = 101003;
const RTM_DRIVE = 101007;
const RTM_EMOTE = 103003;
const RTM_DOCK = 103043;
const RTM_AVOID = 103045;
const RTM_KEEPALIVE = 101005;
const RTM_BATTERY = 101006;   // inbound status: BatteryData {percentage, chargeStatus, adapterStatus, level}
const FRAME_MS = 150;         // how often we sample the LIVE Agora video and push a frame (≈6-7 fps)

// Named eye states -> Air 2 emote emojiId. Mirrors autobot/robot/proto.py EYE_EMOTE_IDS (best-guess catalog;
// refine both together once the emote list is reverse-engineered).
const EYE_IDS: Record<string, number> = {
  neutral: 0, happy: 1, sad: 2, angry: 3, surprised: 4, sleepy: 5, love: 6, dizzy: 7,
  blink: 8, curious: 9, excited: 10, scared: 11, confused: 12, wink: 13, cool: 14,
};
const EYE_PICKS = ["happy", "love", "curious", "surprised", "angry", "sad", "sleepy", "cool"];

export default function ControlPanel({ settings, t, save, feed }: {
  settings: Settings;
  t: Telemetry;
  save: (c: Partial<Settings>) => void;
  feed: FeedItem[];
}) {
  const [status, setStatus] = useState("idle");
  const [connected, setConnected] = useState(false);
  const [heardText, setHeardText] = useState("");
  const videoRef = useRef<HTMLDivElement>(null);
  const rtcRef = useRef<any>(null);
  const rtmRef = useRef<any>(null);
  const sessRef = useRef<Session | null>(null);
  const bridgeWsRef = useRef<WebSocket | null>(null);
  const frameTimerRef = useRef<number | undefined>(undefined);
  const driveTimerRef = useRef<number | undefined>(undefined);
  const keepaliveTimerRef = useRef<number | undefined>(undefined);
  const controllerTimerRef = useRef<number | undefined>(undefined);
  const robotStreamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const vadNodeRef = useRef<ScriptProcessorNode | null>(null);
  const speakingRef = useRef(false);
  const listeningRef = useRef(true);
  const wakeLockRef = useRef<any>(null);
  const wantRef = useRef(false);
  const connectingRef = useRef(false);
  const connectedRef = useRef(false);   // ref (not state) so timers/closures see the live value
  const asleepRef = useRef(settings.asleep);
  const watchdogRef = useRef<number | undefined>(undefined);

  useEffect(() => { asleepRef.current = settings.asleep; }, [settings.asleep]);

  const markConnected = (v: boolean) => { connectedRef.current = v; setConnected(v); };

  const listening = settings.allow_audio_in;
  useEffect(() => { listeningRef.current = listening; }, [listening]);

  useEffect(() => {
    wantRef.current = !settings.asleep;
    if (!settings.asleep) void connect();
    watchdogRef.current = window.setInterval(() => {
      if (wantRef.current && !asleepRef.current && !connectedRef.current && !connectingRef.current) void connect();
    }, 8000);
    const onVis = () => { if (document.visibilityState === "visible" && wantRef.current && !wakeLockRef.current) void acquireWakeLock(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      wantRef.current = false;
      if (watchdogRef.current) clearInterval(watchdogRef.current);
      document.removeEventListener("visibilitychange", onVis);
      void disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Publish a WAV into the Agora call so the robot's own speaker plays it (TTS replies / test tone).
  async function speakViaAgora(text: string) {
    try {
      const rtc = rtcRef.current;
      if (!rtc) { setStatus("not connected"); return; }
      const r = await fetch(`/api/voice/say?text=${encodeURIComponent(text)}`);
      if (!r.ok) { setStatus("say blocked: " + (r.headers.get("X-Reason") || r.status)); return; }
      const buf = await r.arrayBuffer();
      const AgoraRTC = (await import(/* @vite-ignore */ "agora-rtc-sdk-ng")).default as any;
      const track = await AgoraRTC.createBufferSourceAudioTrack({ source: buf });
      speakingRef.current = true;
      await rtc.publish(track);
      track.startProcessAudioBuffer();
      track.on("source-state-change", (state: string) => {
        if (state === "stopped") {
          try { void rtc.unpublish(track); track.close(); } catch { /* */ }
          window.setTimeout(() => { speakingRef.current = false; }, 600);
        }
      });
    } catch (e: any) {
      speakingRef.current = false;
      setStatus("say failed: " + (e?.message || e));
    }
  }

  async function sendUtterance(blob: Blob) {
    if (blob.size < 2400) return;
    setStatus("transcribing…");
    try {
      const r = await fetch("/api/voice/stt", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: blob });
      const j = await r.json();
      const text = String(j?.text || "").trim();
      if (text && text.replace(/[^a-z0-9]/gi, "").length > 1) {
        setHeardText(text);
        setStatus('heard: "' + text + '"');
        await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, speaker: "someone nearby" }) });
      }
    } catch (e: any) {
      setStatus("stt failed: " + (e?.message || e));
    }
  }

  function startHandsFree(agoraAudioTrack: any) {
    try {
      if (vadNodeRef.current) return;
      const mst: MediaStreamTrack | undefined = agoraAudioTrack?.getMediaStreamTrack?.();
      if (!mst) return;
      const stream = new MediaStream([mst]);
      robotStreamRef.current = stream;
      const Ctx = (window.AudioContext || (window as any).webkitAudioContext) as typeof AudioContext;
      const ctx = new Ctx();
      audioCtxRef.current = ctx;
      const src = ctx.createMediaStreamSource(stream);
      const node = ctx.createScriptProcessor(2048, 1, 1);
      vadNodeRef.current = node;
      const THRESH = 0.018, SILENCE_MS = 900, MAX_MS = 12000;
      let speaking = false, silenceStart = 0, startedAt = 0;
      let chunks: BlobPart[] = [];
      let rec: MediaRecorder | null = null;
      const endUtterance = () => { speaking = false; try { rec?.stop(); } catch { /* */ } rec = null; };
      node.onaudioprocess = (ev) => {
        if (!listeningRef.current) { if (speaking) endUtterance(); return; }
        const buf = ev.inputBuffer.getChannelData(0);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        const rms = Math.sqrt(sum / buf.length);
        const now = performance.now();
        const voice = rms > THRESH && !speakingRef.current;
        if (voice) {
          silenceStart = 0;
          if (!speaking) {
            speaking = true; startedAt = now; chunks = [];
            try {
              rec = new MediaRecorder(stream);
              rec.ondataavailable = (e) => chunks.push(e.data);
              rec.onstop = () => { const b = new Blob(chunks, { type: rec?.mimeType || "audio/webm" }); void sendUtterance(b); };
              rec.start();
            } catch { /* */ }
          }
        } else if (speaking) {
          if (!silenceStart) silenceStart = now;
          if (now - silenceStart > SILENCE_MS || now - startedAt > MAX_MS) endUtterance();
        }
      };
      src.connect(node);
      node.connect(ctx.destination);
    } catch { /* */ }
  }

  function stopHandsFree() {
    try { vadNodeRef.current?.disconnect(); } catch { /* */ }
    vadNodeRef.current = null;
    robotStreamRef.current = null;
    try { void audioCtxRef.current?.close(); } catch { /* */ }
    audioCtxRef.current = null;
  }

  async function acquireWakeLock() {
    try {
      const nav = navigator as any;
      if (nav.wakeLock?.request) {
        wakeLockRef.current = await nav.wakeLock.request("screen");
        wakeLockRef.current.addEventListener?.("release", () => { wakeLockRef.current = null; });
      }
    } catch { /* */ }
  }

  // Inbound RTM from the robot: battery/status (101006) -> relay; drive-reject (102) -> resting hint. Also
  // log everything (for reverse-engineering the exact status shapes — see plan: battery-capture).
  function onPeerMessage(message: any) {
    try {
      const raw = message?.text || "{}";
      console.debug("[rtm-in]", raw);
      const j = JSON.parse(raw);
      const d = j?.data ?? j;
      const id = j?.id;
      // battery / charge
      const pct = d?.percentage ?? d?.battery ?? d?.level ?? d?.electric ?? d?.power;
      const chg = d?.chargeStatus ?? d?.adapterStatus ?? d?.charging ?? d?.charge ?? d?.isCharging;
      if (id === RTM_BATTERY || typeof pct === "number") {
        const status: Record<string, unknown> = { connected: true };
        const inner: Record<string, unknown> = {};
        if (typeof pct === "number") inner.battery = pct;
        if (chg !== undefined) inner.charge = (typeof chg === "boolean" ? (chg ? 1 : 0) : Number(chg) > 0 ? 1 : 0);
        if (Object.keys(inner).length) {
          status.status = inner;
          fetch("/api/air2/connected", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(status) }).catch(() => {});
        }
      }
      // sensor telemetry: 6-axis IMU + IR time-of-flight + touch/bump (shapes confirmed via the logging above)
      const sensor: Record<string, unknown> = {};
      const imu = d?.imu ?? d?.accel ?? d?.acceleration ?? d?.sensor;
      if (imu && typeof imu === "object") sensor.imu = imu;
      const gyro = d?.gyro ?? d?.gyroscope;
      if (gyro && typeof gyro === "object") sensor.gyro = gyro;
      const tof = d?.tof ?? d?.distance ?? d?.obstacleDistance ?? d?.range;
      if (typeof tof === "number") sensor.tof = tof;
      for (const k of ["touch", "touched", "bump", "bumped", "collision"]) {
        if (d?.[k] !== undefined) sensor[k] = d[k];
      }
      if (Object.keys(sensor).length) {
        fetch("/api/air2/connected", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connected: true, status: sensor }) }).catch(() => {});
      }
      // drive rejected (docked/charging) — robot refuses to move
      const code = j?.code ?? d?.code ?? d?.result ?? j?.result;
      if (code === 102) {
        fetch("/api/air2/connected", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connected: true, status: { drive_rejected: true } }) }).catch(() => {});
      }
    } catch { /* not JSON */ }
  }

  function setupBridge() {
    if (keepaliveTimerRef.current) clearInterval(keepaliveTimerRef.current);
    keepaliveTimerRef.current = window.setInterval(() => { void sendRtm(RTM_KEEPALIVE, { state: 0 }); }, 2000);
    if (controllerTimerRef.current) clearInterval(controllerTimerRef.current);
    controllerTimerRef.current = window.setInterval(() => {
      const sess = sessRef.current;
      if (sess?.ebo_id) void sendRtm(RTM_LOGIN, { userId: sess.ebo_id });
      void sendRtm(RTM_AVOID, { avoidobstacle: true });
    }, 30000);
    fetch("/api/air2/connected", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connected: true }) }).catch(() => {});
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const bws = new WebSocket(`${proto}://${location.host}/ws`);
    bws.onmessage = (m) => {
      try {
        const e = JSON.parse(m.data);
        if (e?.type !== "air2_cmd") return;
        if (e.cmd === "drive") {
          const rly = -Math.round((e.ly || 0) * 100);
          const rrx = Math.round((e.rx || 0) * 100);
          void sendRtm(RTM_DRIVE, { lx: 0, ly: rly, rx: rrx, ry: 0, buttons: 0 });
          if (e.duration > 0) {
            if (driveTimerRef.current) clearTimeout(driveTimerRef.current);
            driveTimerRef.current = window.setTimeout(() => void stop(), e.duration * 1000);
          }
        } else if (e.cmd === "stop") {
          void stop();
        } else if (e.cmd === "eyes" && e.state) {
          void setEyes(String(e.state));
        } else if (e.cmd === "action") {
          const n = String(e.name || "");
          if (n === "dock") void sendRtm(RTM_DOCK, null);
          else if (n.startsWith("avoid")) void sendRtm(RTM_AVOID, { avoidobstacle: n !== "avoid_off" });
        } else if (e.cmd === "say" && e.text) {
          void speakViaAgora(String(e.text));
        }
      } catch { /* */ }
    };
    bridgeWsRef.current = bws;
    // Sample the LIVE Agora video at ~6-7 fps (not the old 0.7 fps). The brain captions at its own slower
    // pace, but the buffer/SLAM recorder now get a real frame stream. Reuse one canvas; skip if a POST is
    // still in flight so we never queue up.
    const fc = document.createElement("canvas");
    let inflight = false;
    frameTimerRef.current = window.setInterval(() => {
      if (!settings.allow_video || inflight) return;
      const v = videoRef.current?.querySelector("video") as HTMLVideoElement | null;
      if (!v || !v.videoWidth) return;
      fc.width = Math.min(v.videoWidth, 640);
      fc.height = Math.round((fc.width * v.videoHeight) / v.videoWidth);
      fc.getContext("2d")?.drawImage(v, 0, 0, fc.width, fc.height);
      const b64 = fc.toDataURL("image/jpeg", 0.6).split(",")[1];
      inflight = true;
      fetch("/api/air2/frame", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ b64 }) })
        .catch(() => {}).finally(() => { inflight = false; });
    }, FRAME_MS);
  }

  function teardownBridge() {
    fetch("/api/air2/connected", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connected: false }) }).catch(() => {});
    try { bridgeWsRef.current?.close(); } catch { /* */ }
    bridgeWsRef.current = null;
    if (frameTimerRef.current) clearInterval(frameTimerRef.current);
    if (driveTimerRef.current) clearTimeout(driveTimerRef.current);
    if (keepaliveTimerRef.current) clearInterval(keepaliveTimerRef.current);
    if (controllerTimerRef.current) clearInterval(controllerTimerRef.current);
  }

  async function connect() {
    if (connectingRef.current || connectedRef.current) return;
    connectingRef.current = true;
    wantRef.current = true;
    // Tear down any prior Agora clients FIRST so we never join twice with the same uid (UID_CONFLICT).
    try { await rtmRef.current?.logout?.(); } catch { /* */ }
    try { await rtcRef.current?.leave?.(); } catch { /* */ }
    rtmRef.current = null; rtcRef.current = null;
    setStatus("fetching session…");
    let sess: Session;
    try {
      sess = await (await fetch("/api/air2/session")).json();
    } catch (e: any) {
      setStatus("session fetch failed: " + (e?.message || e)); connectingRef.current = false; return;
    }
    if (!sess?.ok) {
      wantRef.current = false;
      setStatus("session error: " + JSON.stringify(sess?.error ?? sess));
      connectingRef.current = false; return;
    }
    sessRef.current = sess;
    try {
      const AgoraRTC = (await import(/* @vite-ignore */ "agora-rtc-sdk-ng")).default as any;
      const rtc = AgoraRTC.createClient({ mode: "rtc", codec: "vp8" });
      rtcRef.current = rtc;
      rtc.on("user-published", async (user: any, mediaType: string) => {
        await rtc.subscribe(user, mediaType);
        if (mediaType === "video" && videoRef.current) user.videoTrack?.play(videoRef.current);
        if (mediaType === "audio") { user.audioTrack?.play(); startHandsFree(user.audioTrack); }
      });
      rtc.on("connection-state-change", (cur: string, _prev: string, reason?: string) => {
        if (cur === "DISCONNECTED" || cur === "FAILED") {
          markConnected(false);
          if (reason && /UID_CONFLICT/i.test(reason)) {
            // someone (a stale join or another tab) holds our uid — fully leave + back off so it frees up.
            setStatus("uid conflict — releasing + retrying…");
            void disconnect().then(() => { wantRef.current = true; });
          }
        }
      });
      rtc.on("exception", (ev: any) => { if (/UID_CONFLICT/i.test(String(ev?.msg || ev?.code || ""))) markConnected(false); });
      await rtc.join(sess.app_id, sess.rtc.channel, sess.rtc.token, Number(sess.rtc.uid));
      setStatus("RTC joined — waiting for robot video…");
    } catch (e: any) {
      const msg = String(e?.message || e);
      setStatus("RTC error: " + msg);
      if (/UID_CONFLICT/i.test(msg)) {
        // free the uid and let the watchdog retry after a short backoff
        try { await rtcRef.current?.leave(); } catch { /* */ }
        rtcRef.current = null; connectingRef.current = false;
        markConnected(false);
        return;
      }
    }
    try {
      const AgoraRTM = (await import(/* @vite-ignore */ "agora-rtm-sdk")).default as any;
      const rtm = AgoraRTM.createInstance(sess.app_id);
      rtmRef.current = rtm;
      await rtm.login({ uid: sess.rtm.uid, token: sess.rtm.token });
      markConnected(true);
      try { rtm.on("MessageFromPeer", onPeerMessage); } catch { /* */ }
      if (sess.ebo_id) await sendRtm(RTM_LOGIN, { userId: sess.ebo_id });
      await sendRtm(RTM_AVOID, { avoidobstacle: true });
      setupBridge();
      void acquireWakeLock();
      setStatus("connected — live");
    } catch (e: any) {
      setStatus("RTM error: " + (e?.message || e));
    } finally {
      connectingRef.current = false;
    }
  }

  async function disconnect() {
    wantRef.current = false;
    try { await wakeLockRef.current?.release?.(); } catch { /* */ }
    wakeLockRef.current = null;
    stopHandsFree();
    teardownBridge();
    try { await rtmRef.current?.logout?.(); } catch { /* */ }
    try { await rtcRef.current?.leave?.(); } catch { /* */ }
    rtmRef.current = null; rtcRef.current = null;
    markConnected(false); setStatus("idle");
  }

  async function sendRtm(id: number, data: Record<string, unknown> | null) {
    const rtm = rtmRef.current; const sess = sessRef.current;
    if (!rtm || !sess) return;
    const msg = JSON.stringify({ id, sid: sess.sid, data: data ?? {}, type: 0, timestamp: Date.now() });
    try { await rtm.sendMessageToPeer({ text: msg }, sess.rtm.robot_uid); } catch { /* */ }
  }

  // Manual drive (always available — bypasses the AI motion gate). Robot forward is negative ly.
  const drive = (ly: number, rx: number) => sendRtm(RTM_DRIVE, { lx: 0, ly: -Math.round(ly * 100), rx: Math.round(rx * 100), ry: 0, buttons: 0 });
  const stop = () => sendRtm(RTM_DRIVE, { lx: 0, ly: 0, rx: 0, ry: 0, buttons: 0 });
  const setEyes = (state: string) => sendRtm(RTM_EMOTE, { voiceIds: [], cycleMode: 0, emojiIds: [EYE_IDS[state] ?? 0], moveIds: [] });

  const batt = typeof t.battery === "number" && t.battery >= 0 ? `${t.battery}%${t.charge === 1 ? " ⚡" : ""}` : "—";
  const MODE_ICON: Record<string, string> = { explore: "🧭", command: "🎯", conversational: "💬" };

  return (
    <div className="flex flex-col gap-3">
      {/* ── VIDEO STAGE: the live feed with HUD overlays ── */}
      <div className="relative hud-frame rounded-xl overflow-hidden border border-line bg-black aspect-video hud-glow">
        <div ref={videoRef} className="absolute inset-0 hud-scan" />

        {/* top-left: mode + connection */}
        <div className="absolute top-2 left-3 flex items-center gap-2 text-[11px] hud-mono z-10">
          <span className={`flex items-center gap-1 px-2 py-0.5 rounded border ${connected ? "border-accent/60 text-accent" : "border-line text-mut"}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${connected ? "bg-accent reactor" : "bg-mut"}`} />
            {connected ? "LIVE" : "LINK…"}
          </span>
          <span className="px-2 py-0.5 rounded border border-gold/50 text-gold uppercase tracking-wider">
            {MODE_ICON[settings.mode] ?? "•"} {settings.mode}
          </span>
        </div>

        {/* top-right: battery / state */}
        <div className="absolute top-2 right-3 flex items-center gap-2 text-[11px] hud-mono z-10">
          {t.resting && <span className="text-warn">⚡ RESTING</span>}
          <span className={`px-2 py-0.5 rounded border ${typeof t.battery === "number" && t.battery <= 20 ? "border-bad/60 text-bad" : "border-line text-fg"}`}>
            ▮ {batt}
          </span>
        </div>

        {/* bottom: terminator-style last-2 cognition feed */}
        <div className="absolute bottom-0 left-0 right-0 px-3 py-2 bg-gradient-to-t from-black/85 to-transparent z-10">
          <TerminatorFeed feed={feed} n={2} />
        </div>

        {settings.asleep && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/70 z-20 text-mut text-sm">
            🌙 FreeBo is asleep — press Wake
          </div>
        )}
      </div>

      {/* connection / sleep row */}
      <div className="flex gap-2 items-center text-xs hud-mono">
        <span className="text-mut flex-1 truncate">{status}</span>
        {settings.asleep ? (
          <button
            onClick={() => { asleepRef.current = false; wantRef.current = true; void api.sleep(false); void connect(); }}
            className="bg-accent/20 border border-accent text-fg rounded-lg py-1 px-3 active:scale-95 hud-glow"
          >☀ Wake</button>
        ) : (
          <button
            onClick={() => { asleepRef.current = true; wantRef.current = false; void api.sleep(true); void disconnect(); }}
            className="bg-card2 border border-line rounded-lg py-1 px-3 active:scale-95"
            title="Go dark: cut ALL robot comms + stop the AI (Wake to resume)"
          >🌙 Sleep</button>
        )}
        {connected
          ? <button onClick={() => disconnect()} className="bg-bad/80 text-white border border-bad rounded-lg py-1 px-3 active:scale-95">Disconnect</button>
          : <button onClick={() => connect()} className="bg-accent/20 border border-accent text-fg rounded-lg py-1 px-3 active:scale-95 hud-glow">Connect</button>}
      </div>

      {/* Manual drive + eyes side by side */}
      <div className="grid grid-cols-[auto_1fr] gap-4 items-start">
        <div className="flex flex-col items-center gap-2">
          <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow self-start">Manual</div>
          <Joystick maxSpeed={settings.max_speed} onDrive={(ly, rx) => drive(ly, rx)} onStop={stop} disabled={!connected} />
        </div>
        <div className="flex flex-col gap-2">
          <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow">Eyes</div>
          <div className="flex flex-wrap gap-1.5">
            {EYE_PICKS.map((s) => (
              <button key={s} onClick={() => setEyes(s)} disabled={!connected}
                className="bg-card2/60 border border-line rounded-lg py-1.5 px-2.5 text-xs active:scale-95 disabled:opacity-40 capitalize hover:border-accent/50">
                {s}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap gap-1.5 mt-1">
            <button onClick={() => sendRtm(RTM_DOCK, null)} disabled={!connected} className="bg-card2/60 border border-line rounded-lg py-1.5 px-2.5 text-xs active:scale-95 disabled:opacity-40 hover:border-accent/50">⊟ Dock</button>
            <button onClick={() => sendRtm(RTM_AVOID, { avoidobstacle: true })} disabled={!connected} className="bg-card2/60 border border-line rounded-lg py-1.5 px-2.5 text-xs active:scale-95 disabled:opacity-40 hover:border-accent/50">⛨ Avoid</button>
            <button onClick={() => speakViaAgora("__test__")} disabled={!connected} className="bg-card2/60 border border-line rounded-lg py-1.5 px-2.5 text-xs active:scale-95 disabled:opacity-40 hover:border-accent/50">🔊 Test</button>
          </div>
        </div>
      </div>

      {heardText && <div className="text-xs text-accent hud-mono">🗣 "{heardText}"</div>}
      <div className="text-[11px] text-mut">
        Hands-free: talk near the robot — it hears through its own mic ({listening ? "listening" : "muted — enable Hear"}).
        Manual joystick + eyes always work, even with autonomy off.
      </div>
    </div>
  );
}
