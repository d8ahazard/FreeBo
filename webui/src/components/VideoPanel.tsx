import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Telemetry } from "../types";

/**
 * Video: try WebRTC (WHEP, proxied same-origin by the brain). If ICE fails (e.g. mock bridge, or no
 * media), fall back to a periodic JPEG snapshot so the UI always shows what the robot sees.
 * Ported from the upstream ebo.html player logic; see docs/PROVENANCE.md.
 */
export default function VideoPanel({ t }: { t: Telemetry }) {
  const vidRef = useRef<HTMLVideoElement>(null);
  const pcRef = useRef<RTCPeerConnection | null>(null);
  const [mode, setMode] = useState<"connecting" | "webrtc" | "snapshot" | "idle">("idle");
  const [snap, setSnap] = useState<string>("");

  const awake = !!t.awake && !!t.connected && !t.paused;

  useEffect(() => {
    if (!awake) {
      setMode("idle");
      pcRef.current?.close();
      pcRef.current = null;
      return;
    }
    if (mode !== "idle") return;
    let cancelled = false;
    setMode("connecting");
    (async () => {
      try {
        const pc = new RTCPeerConnection();
        pc.addTransceiver("video", { direction: "recvonly" });
        pc.ontrack = (e) => {
          if (vidRef.current) vidRef.current.srcObject = e.streams[0];
        };
        await pc.setLocalDescription(await pc.createOffer());
        await new Promise<void>((res) => {
          if (pc.iceGatheringState === "complete") return res();
          const tmr = setTimeout(res, 1500);
          pc.addEventListener("icegatheringstatechange", () => {
            if (pc.iceGatheringState === "complete") {
              clearTimeout(tmr);
              res();
            }
          });
        });
        const resp = await fetch("/whep", {
          method: "POST",
          headers: { "Content-Type": "application/sdp" },
          body: pc.localDescription!.sdp,
        });
        if (!resp.ok) throw new Error("whep " + resp.status);
        await pc.setRemoteDescription({ type: "answer", sdp: await resp.text() });
        if (cancelled) {
          pc.close();
          return;
        }
        pcRef.current = pc;
        setMode("webrtc");
      } catch {
        if (!cancelled) setMode("snapshot");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [awake, mode]);

  // snapshot fallback poller
  useEffect(() => {
    if (mode !== "snapshot") return;
    const tick = () => setSnap(api.snapshotUrl());
    tick();
    const iv = setInterval(tick, 1500);
    return () => clearInterval(iv);
  }, [mode]);

  return (
    <div className="bg-black rounded-xl overflow-hidden aspect-video border border-line relative">
      <video ref={vidRef} autoPlay muted playsInline className="w-full h-full object-contain bg-black" />
      {mode === "snapshot" && snap && (
        <img src={snap} alt="robot view" className="absolute inset-0 w-full h-full object-contain" />
      )}
      {!awake && (
        <div className="absolute inset-0 flex items-center justify-center text-mut text-sm">
          {t.paused ? "session released to the Enabot app" : t.connected ? "robot asleep — press Wake" : "bridge offline"}
        </div>
      )}
      <div className="absolute top-2 left-2 text-[10px] font-mono px-2 py-0.5 rounded bg-black/50 text-mut">
        {mode === "webrtc" ? "WebRTC" : mode === "snapshot" ? "snapshot" : mode}
      </div>
    </div>
  );
}
