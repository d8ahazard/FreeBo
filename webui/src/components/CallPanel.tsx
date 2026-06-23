import { useEffect, useRef, useState } from "react";
import VideoPanel from "./VideoPanel";
import type { Settings, Telemetry } from "../types";
import { MulawPlayer, bytesToB64, b64ToBytes, float32ToMulaw8k } from "../audio";

/**
 * 2-way "video call": the live camera (existing WHEP stream via VideoPanel) plus full-duplex audio over a
 * dedicated /ws/call socket. The robot has no screen, so — like the official app — "video call" means you
 * see the robot's camera and both sides talk. Mic audio is encoded to G.711 µ-law @ 8 kHz in the browser and
 * sent to the robot speaker (talk must be enabled); inbound robot mic audio is played back.
 */
export default function CallPanel({ settings, t }: { settings: Settings; t: Telemetry }) {
  const [inCall, setInCall] = useState(false);
  const [micOn, setMicOn] = useState(false);
  const [status, setStatus] = useState("");
  const wsRef = useRef<WebSocket | null>(null);
  const playerRef = useRef<MulawPlayer | null>(null);
  const mediaRef = useRef<{ stream: MediaStream; ctx: AudioContext; node: ScriptProcessorNode } | null>(null);

  const cleanupMic = () => {
    const m = mediaRef.current;
    if (m) {
      try { m.node.disconnect(); } catch { /* */ }
      try { m.ctx.close(); } catch { /* */ }
      m.stream.getTracks().forEach((tr) => tr.stop());
    }
    mediaRef.current = null;
    setMicOn(false);
  };

  const hangUp = () => {
    cleanupMic();
    wsRef.current?.close();
    wsRef.current = null;
    playerRef.current?.close();
    playerRef.current = null;
    setInCall(false);
    setStatus("");
  };

  useEffect(() => () => hangUp(), []); // cleanup on unmount

  const startCall = () => {
    if (wsRef.current) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/call`);
    ws.onopen = () => { setInCall(true); setStatus("connected"); playerRef.current = new MulawPlayer(); };
    ws.onclose = () => { setInCall(false); };
    ws.onmessage = (m) => {
      try {
        const msg = JSON.parse(m.data);
        if (msg.type === "audio" && msg.b64) {
          playerRef.current?.resume();
          playerRef.current?.play(b64ToBytes(msg.b64));
        } else if (msg.type === "blocked") {
          setStatus(msg.reason || "blocked");
        }
      } catch { /* ignore */ }
    };
    wsRef.current = ws;
  };

  const startMic = async () => {
    if (!settings.talk_enabled) { setStatus("Enable 'Allow the robot to talk' in Config to speak."); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
      const source = ctx.createMediaStreamSource(stream);
      const node = ctx.createScriptProcessor(4096, 1, 1);
      node.onaudioprocess = (e) => {
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        const mulaw = float32ToMulaw8k(e.inputBuffer.getChannelData(0), ctx.sampleRate);
        ws.send(JSON.stringify({ type: "say_audio", b64: bytesToB64(mulaw) }));
      };
      source.connect(node);
      node.connect(ctx.destination);
      mediaRef.current = { stream, ctx, node };
      setMicOn(true);
      setStatus("talking");
    } catch (err: any) {
      setStatus(`mic error: ${err?.message || err}`);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="text-[11px] uppercase tracking-wider text-mut">2-way call</div>
      <VideoPanel t={t} />
      <div className="flex flex-wrap gap-2 items-center">
        {!inCall ? (
          <button onClick={startCall} className="bg-accent border border-accent rounded-lg py-2 px-4 text-sm font-medium active:scale-95">📞 Start call</button>
        ) : (
          <>
            {!micOn ? (
              <button onClick={startMic} className="bg-accent border border-accent rounded-lg py-2 px-4 text-sm active:scale-95">🎙 Speak</button>
            ) : (
              <button onClick={cleanupMic} className="bg-warn/80 border border-warn rounded-lg py-2 px-4 text-sm active:scale-95">🔇 Mute mic</button>
            )}
            <button onClick={hangUp} className="bg-bad text-white border border-bad rounded-lg py-2 px-4 text-sm font-medium active:scale-95">Hang up</button>
          </>
        )}
        <span className="text-xs text-mut">{status}</span>
      </div>
      {!settings.talk_enabled && (
        <div className="text-xs text-warn">Talk is off — you'll hear the robot, but enable "Allow the robot to talk" in Config to speak back.</div>
      )}
      <div className="text-xs text-mut">You hear the robot's mic; pressing Speak sends your voice to its speaker (G.711 8 kHz).</div>
    </div>
  );
}
