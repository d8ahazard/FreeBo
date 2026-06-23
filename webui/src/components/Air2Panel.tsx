import { useEffect, useRef, useState } from "react";

/**
 * EBO Air 2 / Max live control — the cloud (Agora) path.
 *
 * The Air 2 is cloud-controlled: video/audio over Agora RTC, drive/commands over Agora RTM. FreeBo's backend
 * (/api/air2/session) does the signed Enabot REST + session creation and hands us the Agora app id, channel,
 * uids and short-lived tokens (see autobot/robot/ebo_cloud.py + docs/AIR2_CLOUD.md). Here we:
 *   - join the RTC channel and render the robot's video (and snapshot it),
 *   - log into RTM and send the recovered control JSON (drive=101007, dock=103043, ...) to the robot peer,
 *   - relay Grok's commands (over /ws) into RTM and stream camera frames back for perception,
 *   - listen hands-free: continuous mic + voice-activity-detection -> Whisper -> Grok (no button to hold).
 *
 * Agora's web SDKs run in the browser; they're loaded dynamically so the app still builds without them.
 * Tokens are short-lived, so we fetch a fresh session on connect, and we auto-(re)connect so the robot is
 * live and roaming the moment this tab is open.
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
const RTM_DOCK = 103043;
const RTM_AVOID = 103045;
const RTM_KEEPALIVE = 101005;   // the app pings this ~every 2s; without it the robot drops the session

export default function Air2Panel() {
  const [status, setStatus] = useState("idle");
  const [connected, setConnected] = useState(false);
  const [listening, setListening] = useState(true);   // hands-free mic on by default
  const [heardText, setHeardText] = useState("");
  const videoRef = useRef<HTMLDivElement>(null);
  const rtcRef = useRef<any>(null);
  const rtmRef = useRef<any>(null);
  const sessRef = useRef<Session | null>(null);
  // --- brain bridge: relay Grok's commands (over /ws) to Agora RTM, and stream frames back ---
  const bridgeWsRef = useRef<WebSocket | null>(null);
  const frameTimerRef = useRef<number | undefined>(undefined);
  const driveTimerRef = useRef<number | undefined>(undefined);
  const keepaliveTimerRef = useRef<number | undefined>(undefined);
  const controllerTimerRef = useRef<number | undefined>(undefined);
  // --- hands-free listening (on the ROBOT's mic, received over Agora — there is no local mic) ---
  const robotStreamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const vadNodeRef = useRef<ScriptProcessorNode | null>(null);
  const speakingRef = useRef(false);     // true while the robot is speaking (gate the mic to avoid self-hearing)
  const listeningRef = useRef(true);
  const wakeLockRef = useRef<any>(null);
  // --- auto-(re)connect ---
  const wantRef = useRef(false);
  const connectingRef = useRef(false);
  const watchdogRef = useRef<number | undefined>(undefined);

  useEffect(() => { listeningRef.current = listening; }, [listening]);

  // Auto-connect on mount; auto-reconnect via watchdog; clean up on unmount.
  useEffect(() => {
    wantRef.current = true;
    void connect();
    watchdogRef.current = window.setInterval(() => {
      if (wantRef.current && !connected && !connectingRef.current) void connect();
    }, 6000);
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

  // Grok's reply (or any say) -> fetch server TTS WAV -> publish into the Agora call so the robot speaks it.
  async function speakViaAgora(text: string) {
    try {
      const rtc = rtcRef.current;
      if (!rtc) return;
      const buf = await (await fetch(`/api/voice/say?text=${encodeURIComponent(text)}`)).arrayBuffer();
      const AgoraRTC = (await import(/* @vite-ignore */ "agora-rtc-sdk-ng")).default as any;
      const track = await AgoraRTC.createBufferSourceAudioTrack({ source: buf });
      speakingRef.current = true;   // suppress the mic while we talk (avoid hearing ourselves)
      await rtc.publish(track);
      track.startProcessAudioBuffer();
      track.on("source-state-change", (state: string) => {
        if (state === "stopped") {
          try { void rtc.unpublish(track); track.close(); } catch { /* */ }
          // brief tail so the room echo dies down before we listen again
          window.setTimeout(() => { speakingRef.current = false; }, 600);
        }
      });
    } catch (e: any) {
      speakingRef.current = false;
      setStatus("say failed: " + (e?.message || e));
    }
  }

  // Send a captured utterance to Whisper, then feed the transcript to Grok (/api/chat).
  async function sendUtterance(blob: Blob) {
    if (blob.size < 2400) return;   // too short -> noise, ignore
    setStatus("transcribing…");
    try {
      const r = await fetch("/api/voice/stt", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: blob });
      const j = await r.json();
      const text = String(j?.text || "").trim();
      if (text && text.replace(/[^a-z0-9]/gi, "").length > 1) {
        setHeardText(text);
        setStatus('you said: "' + text + '"');
        await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      } else {
        setStatus("(didn't catch that)");
      }
    } catch (e: any) {
      setStatus("stt failed: " + (e?.message || e));
    }
  }

  // Hands-free: listen to the ROBOT's microphone, which arrives as a remote Agora audio track (there is no
  // local mic on this machine). We watch its volume; when someone talks we record until a ~0.9s pause, then
  // ship the clip to Whisper. Gated while the robot itself is speaking (so it doesn't transcribe its own TTS).
  // VAD runs from a Web Audio ScriptProcessor callback (not requestAnimationFrame) so it KEEPS RUNNING when
  // the tab is in the background — essential for an unattended overnight robot.
  function startHandsFree(agoraAudioTrack: any) {
    try {
      if (vadNodeRef.current) return;   // already listening
      const mst: MediaStreamTrack | undefined = agoraAudioTrack?.getMediaStreamTrack?.();
      if (!mst) { setStatus("no robot audio track yet"); return; }
      const stream = new MediaStream([mst]);
      robotStreamRef.current = stream;
      const Ctx = (window.AudioContext || (window as any).webkitAudioContext) as typeof AudioContext;
      const ctx = new Ctx();
      audioCtxRef.current = ctx;
      const src = ctx.createMediaStreamSource(stream);
      const node = ctx.createScriptProcessor(2048, 1, 1);
      vadNodeRef.current = node;

      const THRESH = 0.018;     // RMS speech threshold
      const SILENCE_MS = 900;   // pause that ends an utterance
      const MAX_MS = 12000;     // hard cap on a single utterance
      let speaking = false;
      let silenceStart = 0;
      let startedAt = 0;
      let chunks: BlobPart[] = [];
      let rec: MediaRecorder | null = null;

      const endUtterance = () => {
        speaking = false;
        try { rec?.stop(); } catch { /* */ }
        rec = null;
      };

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
            speaking = true;
            startedAt = now;
            chunks = [];
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
      node.connect(ctx.destination);   // required for onaudioprocess to fire
    } catch (e: any) {
      setStatus("mic error: " + (e?.message || e));
    }
  }

  function stopHandsFree() {
    try { vadNodeRef.current?.disconnect(); } catch { /* */ }
    vadNodeRef.current = null;
    // Do NOT stop the tracks — they belong to Agora (the robot's audio); stopping would kill playback.
    robotStreamRef.current = null;
    try { void audioCtxRef.current?.close(); } catch { /* */ }
    audioCtxRef.current = null;
  }

  // Keep the screen/tab awake so the OS doesn't suspend the page (which would freeze the bridge overnight).
  async function acquireWakeLock() {
    try {
      const nav = navigator as any;
      if (nav.wakeLock?.request) {
        wakeLockRef.current = await nav.wakeLock.request("screen");
        wakeLockRef.current.addEventListener?.("release", () => { wakeLockRef.current = null; });
      }
    } catch { /* not supported / denied — best effort */ }
  }

  function setupBridge() {
    // keepalive: the robot drops the control session without periodic pings (the app sends 101005 ~every 2s)
    if (keepaliveTimerRef.current) clearInterval(keepaliveTimerRef.current);
    keepaliveTimerRef.current = window.setInterval(() => { void sendRtm(RTM_KEEPALIVE, { state: 0 }); }, 2000);
    // Re-assert that we're the active controller + obstacle avoidance every 30s. Over a long unattended run
    // the robot can drop our controller binding and start rejecting drive commands (RTM error 102); this
    // keeps the drive authority alive so it doesn't silently stop moving.
    if (controllerTimerRef.current) clearInterval(controllerTimerRef.current);
    controllerTimerRef.current = window.setInterval(() => {
      const sess = sessRef.current;
      if (sess?.ebo_id) void sendRtm(RTM_LOGIN, { userId: sess.ebo_id });
      void sendRtm(RTM_AVOID, { avoidobstacle: true });
    }, 30000);
    // tell the backend the tab is connected, so the brain knows it can relay
    fetch("/api/air2/connected", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connected: true }) }).catch(() => {});
    // listen for the brain's air2_cmd events
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const bws = new WebSocket(`${proto}://${location.host}/ws`);
    bws.onmessage = (m) => {
      try {
        const e = JSON.parse(m.data);
        if (e?.type !== "air2_cmd") return;
        if (e.cmd === "drive") {
          // brain ly>0 = forward; the robot's forward is negative ly (matches the working d-pad)
          const rly = -Math.round((e.ly || 0) * 100);
          const rrx = Math.round((e.rx || 0) * 100);
          void sendRtm(RTM_DRIVE, { lx: 0, ly: rly, rx: rrx, ry: 0, buttons: 0 });
          if (e.duration > 0) {
            if (driveTimerRef.current) clearTimeout(driveTimerRef.current);
            driveTimerRef.current = window.setTimeout(() => void stop(), e.duration * 1000);
          }
        } else if (e.cmd === "stop") {
          void stop();
        } else if (e.cmd === "action") {
          const n = String(e.name || "");
          if (n === "dock") void sendRtm(RTM_DOCK, null);
          else if (n.startsWith("avoid")) void sendRtm(RTM_AVOID, { avoidobstacle: n !== "avoid_off" });
        } else if (e.cmd === "say" && e.text) {
          void speakViaAgora(String(e.text));
        }
      } catch { /* ignore */ }
    };
    bridgeWsRef.current = bws;
    // stream a camera frame to the brain every ~1.5s
    frameTimerRef.current = window.setInterval(() => {
      const v = videoRef.current?.querySelector("video") as HTMLVideoElement | null;
      if (!v || !v.videoWidth) return;
      const c = document.createElement("canvas");
      c.width = Math.min(v.videoWidth, 640);
      c.height = Math.round((c.width * v.videoHeight) / v.videoWidth);
      c.getContext("2d")?.drawImage(v, 0, 0, c.width, c.height);
      const b64 = c.toDataURL("image/jpeg", 0.5).split(",")[1];
      fetch("/api/air2/frame", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ b64 }) }).catch(() => {});
    }, 1500);
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
    if (connectingRef.current || connected) return;
    connectingRef.current = true;
    wantRef.current = true;
    setStatus("fetching session…");
    let sess: Session;
    try {
      sess = await (await fetch("/api/air2/session")).json();
    } catch (e: any) {
      setStatus("session fetch failed: " + (e?.message || e)); connectingRef.current = false; return;
    }
    if (!sess?.ok) {
      // Bad/missing creds: don't hammer-retry forever — stop wanting until the user acts.
      wantRef.current = false;
      setStatus("session error: " + JSON.stringify(sess?.error ?? sess));
      connectingRef.current = false; return;
    }
    sessRef.current = sess;

    // ---- Agora RTC (video) ----
    try {
      const AgoraRTC = (await import(/* @vite-ignore */ "agora-rtc-sdk-ng")).default as any;
      const rtc = AgoraRTC.createClient({ mode: "rtc", codec: "vp8" });
      rtcRef.current = rtc;
      rtc.on("user-published", async (user: any, mediaType: string) => {
        await rtc.subscribe(user, mediaType);
        if (mediaType === "video" && videoRef.current) user.videoTrack?.play(videoRef.current);
        if (mediaType === "audio") {
          user.audioTrack?.play();                 // hear the room on our speakers
          startHandsFree(user.audioTrack);         // and run VAD/STT on the robot's mic
        }
      });
      rtc.on("connection-state-change", (cur: string) => {
        if (cur === "DISCONNECTED" || cur === "FAILED") { setConnected(false); }
      });
      await rtc.join(sess.app_id, sess.rtc.channel, sess.rtc.token, Number(sess.rtc.uid));
      setStatus("RTC joined — waiting for robot video…");
    } catch (e: any) {
      setStatus("RTC error: " + (e?.message || e));
    }

    // ---- Agora RTM (control) ----
    try {
      const AgoraRTM = (await import(/* @vite-ignore */ "agora-rtm-sdk")).default as any;
      const rtm = AgoraRTM.createInstance(sess.app_id);
      rtmRef.current = rtm;
      await rtm.login({ uid: sess.rtm.uid, token: sess.rtm.token });
      setConnected(true);
      // Relay any status the robot sends (battery, etc.) back to the backend so auto-dock can work.
      try {
        rtm.on("MessageFromPeer", (message: any) => {
          try {
            const j = JSON.parse(message?.text || "{}");
            const d = j?.data ?? j;
            const batt = d?.battery ?? d?.electric ?? d?.power ?? d?.elec;
            if (typeof batt === "number") {
              const charge = (d?.charging ?? d?.charge ?? d?.isCharging) ? 1 : 0;
              fetch("/api/air2/connected", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connected: true, status: { battery: batt, charge } }) }).catch(() => {});
            }
          } catch { /* not JSON / not status */ }
        });
      } catch { /* */ }
      // register as the controller (the app sends 101003 {userId} right after creating the session)
      if (sess.ebo_id) await sendRtm(RTM_LOGIN, { userId: sess.ebo_id });
      await sendRtm(RTM_AVOID, { avoidobstacle: true });   // onboard collision avoidance ON so it can roam safely
      setupBridge();          // let Grok drive + see through this tab + keepalive
      // hands-free listening starts when the robot's audio track arrives (see user-published handler)
      void acquireWakeLock();  // keep the page from being suspended overnight
      setStatus("connected — roaming + listening (Grok bridge active)");
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
    setConnected(false); setStatus("idle");
  }

  async function sendRtm(id: number, data: Record<string, unknown> | null) {
    const rtm = rtmRef.current; const sess = sessRef.current;
    if (!rtm || !sess) return;
    const msg = JSON.stringify({ id, sid: sess.sid, data: data ?? {}, type: 0, timestamp: Date.now() });
    try {
      await rtm.sendMessageToPeer({ text: msg }, sess.rtm.robot_uid);
    } catch (e: any) {
      setStatus("send failed: " + (e?.message || e));
    }
  }

  // Drive: {lx,ly,rx,ry,buttons} ints -100..100. ly = forward axis, rx = turn. Flip signs if reversed on your unit.
  const drive = (ly: number, rx: number) => sendRtm(RTM_DRIVE, { lx: 0, ly, rx, ry: 0, buttons: 0 });
  const stop = () => sendRtm(RTM_DRIVE, { lx: 0, ly: 0, rx: 0, ry: 0, buttons: 0 });
  const SP = 90;

  const snapshot = () => {
    const v = videoRef.current?.querySelector("video") as HTMLVideoElement | null;
    if (!v) { setStatus("no video to snapshot yet"); return; }
    const c = document.createElement("canvas");
    c.width = v.videoWidth; c.height = v.videoHeight;
    c.getContext("2d")?.drawImage(v, 0, 0);
    const a = document.createElement("a");
    a.href = c.toDataURL("image/png"); a.download = `freebo-${Date.now()}.png`; a.click();
  };

  const pad = "aspect-square text-xl rounded-lg bg-card2 border border-line active:scale-95 disabled:opacity-40";

  return (
    <div className="flex flex-col gap-4">
      <div className="text-[11px] uppercase tracking-wider text-mut">EBO Air 2 — live (cloud)</div>
      <div ref={videoRef} className="bg-black rounded-xl aspect-video overflow-hidden border border-line" />
      <div className="flex gap-2 items-center flex-wrap">
        {!connected ? (
          <button onClick={() => connect()} className="bg-accent border border-accent rounded-lg py-2 px-4 text-sm font-medium active:scale-95">Connect</button>
        ) : (
          <>
            <button
              onClick={() => setListening((v) => !v)}
              className={`rounded-lg py-2 px-3 text-sm font-medium active:scale-95 border ${listening ? "bg-accent border-accent" : "bg-card2 border-line"}`}
            >
              {listening ? "🎙 Listening" : "🔇 Mic off"}
            </button>
            <button onClick={snapshot} className="bg-card2 border border-line rounded-lg py-2 px-3 text-sm active:scale-95">📷 Snapshot</button>
            <button onClick={() => disconnect()} className="bg-bad text-white border border-bad rounded-lg py-2 px-4 text-sm active:scale-95">Disconnect</button>
          </>
        )}
        <span className="text-xs text-mut flex-1">{status}</span>
      </div>

      {heardText && <div className="text-xs text-fg">🗣 "{heardText}"</div>}

      {connected && (
        <div className="grid grid-cols-3 gap-2 max-w-[240px] self-center">
          <span /><button className={pad} onMouseDown={() => drive(-SP, 0)} onMouseUp={stop} onMouseLeave={stop}>▲</button><span />
          <button className={pad} onMouseDown={() => drive(0, -SP)} onMouseUp={stop} onMouseLeave={stop}>◀</button>
          <button className={pad} onClick={stop}>■</button>
          <button className={pad} onMouseDown={() => drive(0, SP)} onMouseUp={stop} onMouseLeave={stop}>▶</button>
          <span /><button className={pad} onMouseDown={() => drive(SP, 0)} onMouseUp={stop} onMouseLeave={stop}>▼</button><span />
        </div>
      )}
      {connected && (
        <div className="flex gap-2 justify-center">
          <button onClick={() => sendRtm(RTM_DOCK, null)} className="bg-card2 border border-line rounded-lg py-1.5 px-3 text-sm active:scale-95">Dock</button>
          <button onClick={() => sendRtm(RTM_AVOID, { avoidobstacle: true })} className="bg-card2 border border-line rounded-lg py-1.5 px-3 text-sm active:scale-95">Avoid on</button>
        </div>
      )}
      <div className="text-xs text-mut">Hands-free: talk near the robot ("hey, what's up") — the robot's mic feeds Whisper and the brain replies through the robot's speaker. Toggle 🎙 to mute listening.</div>
      <div className="text-xs text-mut">Obstacle avoidance is enabled on connect so Grok can roam without crashing. Hold a d-pad button to drive manually (release = stop).</div>
    </div>
  );
}
