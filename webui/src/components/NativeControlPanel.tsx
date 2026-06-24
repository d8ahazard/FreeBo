import { useState } from "react";
import { api } from "../api";
import type { FeedItem, Settings, Telemetry } from "../types";
import Joystick from "./Joystick";
import { TerminatorFeed } from "./ThoughtFeed";

/**
 * NativeControlPanel — the control surface for the FULLY NATIVE Air 2 link (robot_link = air2_native). The
 * server owns Agora (RTM control + RTC video) headless, so the browser does NO Agora here (doing so would
 * clash uids with the server). Video is the server's MJPEG; manual drive/eyes/dock go through /api/control.
 */
const EYE_PICKS = ["happy", "love", "curious", "surprised", "angry", "sad", "sleepy", "cool"];
const MODE_ICON: Record<string, string> = { observe: "👁", explore: "🧭", command: "🎯", conversational: "💬" };

export default function NativeControlPanel({ settings, t, feed, motionLocked = false }: {
  settings: Settings;
  t: Telemetry;
  save: (c: Partial<Settings>) => void;
  feed: FeedItem[];
  motionLocked?: boolean;
}) {
  const [imgErr, setImgErr] = useState(false);
  const connected = !!t.connected;
  const moveDisabled = !connected || motionLocked;   // E-STOP latch disables all manual motion
  const batt = typeof t.battery === "number" && t.battery >= 0 ? `${t.battery}%${t.charge === 1 ? " ⚡" : ""}` : "—";

  return (
    <div className="flex flex-col gap-3">
      {/* VIDEO STAGE — server MJPEG */}
      <div className="relative hud-frame rounded-xl overflow-hidden border border-line bg-black aspect-video hud-glow">
        {!settings.asleep && (
          <img
            src={api.mjpegUrl()}
            alt="robot camera"
            onError={() => setImgErr(true)}
            onLoad={() => setImgErr(false)}
            className="absolute inset-0 w-full h-full object-contain hud-scan"
          />
        )}

        <div className="absolute top-2 left-3 flex items-center gap-2 text-[11px] hud-mono z-10">
          <span className={`flex items-center gap-1 px-2 py-0.5 rounded border ${connected ? "border-accent/60 text-accent" : "border-line text-mut"}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${connected ? "bg-accent reactor" : "bg-mut"}`} />
            {connected ? "LIVE · NATIVE" : "LINK…"}
          </span>
          <span className="px-2 py-0.5 rounded border border-gold/50 text-gold uppercase tracking-wider">
            {MODE_ICON[settings.mode] ?? "•"} {settings.mode}
          </span>
        </div>
        <div className="absolute top-2 right-3 flex items-center gap-2 text-[11px] hud-mono z-10">
          {t.resting && <span className="text-warn">⚡ RESTING</span>}
          <span className={`px-2 py-0.5 rounded border ${typeof t.battery === "number" && t.battery <= 20 ? "border-bad/60 text-bad" : "border-line text-fg"}`}>▮ {batt}</span>
        </div>

        <div className="absolute bottom-0 left-0 right-0 px-3 py-2 bg-gradient-to-t from-black/85 to-transparent z-10">
          <TerminatorFeed feed={feed} n={2} />
        </div>

        {settings.asleep && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/70 z-20 text-mut text-sm">🌙 DARK — comms cut + AI stopped. Press Wake to resume.</div>
        )}
        {imgErr && !settings.asleep && (
          <div className="absolute inset-0 flex items-center justify-center text-mut text-xs hud-mono z-[5]">▸ awaiting native video feed…</div>
        )}
      </div>

      {/* status + sleep */}
      <div className="flex gap-2 items-center text-xs hud-mono">
        <span className="text-mut flex-1 truncate">
          {connected ? "server-side RTM + RTC · no browser" : "native link connecting…"}
          {typeof t.video_frames === "number" ? ` · ${t.video_frames} frames` : ""}
        </span>
        {settings.asleep ? (
          <button onClick={() => api.sleep(false)} className="bg-accent/20 border border-accent text-fg rounded-lg py-1 px-3 active:scale-95 hud-glow">☀ Wake</button>
        ) : (
          <button onClick={() => api.sleep(true)} className="bg-card2 border border-line rounded-lg py-1 px-3 active:scale-95" title="Go dark: cut ALL robot comms + stop the AI (Wake to resume)">🌙 Sleep</button>
        )}
      </div>

      {/* manual drive + eyes */}
      <div className="grid grid-cols-[auto_1fr] gap-4 items-start">
        <div className="flex flex-col items-center gap-2">
          <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow self-start">Manual</div>
          <Joystick
            maxSpeed={settings.max_speed}
            onDrive={(ly, rx) => api.drive(ly, rx)}
            onStop={() => api.stop()}
            disabled={moveDisabled}
          />
          {motionLocked && <div className="text-[10px] text-bad hud-mono">E-STOP latched · reset to drive</div>}
        </div>
        <div className="flex flex-col gap-2">
          <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow">Eyes</div>
          <div className="flex flex-wrap gap-1.5">
            {EYE_PICKS.map((s) => (
              <button key={s} onClick={() => api.action(`eyes_${s}`)} disabled={!connected}
                className="bg-card2/60 border border-line rounded-lg py-1.5 px-2.5 text-xs active:scale-95 disabled:opacity-40 capitalize hover:border-accent/50">
                {s}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap gap-1.5 mt-1">
            <button onClick={() => api.action("dock")} disabled={moveDisabled} className="bg-card2/60 border border-line rounded-lg py-1.5 px-2.5 text-xs active:scale-95 disabled:opacity-40 hover:border-accent/50">⊟ Dock</button>
            <button onClick={() => api.action("avoid_on")} disabled={!connected} className="bg-card2/60 border border-line rounded-lg py-1.5 px-2.5 text-xs active:scale-95 disabled:opacity-40 hover:border-accent/50">⛨ Avoid</button>
            <button
              onClick={() => api.action(t.laser ? "laser_off" : "laser_on")}
              disabled={!connected}
              className={`border rounded-lg py-1.5 px-2.5 text-xs active:scale-95 disabled:opacity-40 ${t.laser ? "bg-bad/20 border-bad text-bad" : "bg-card2/60 border-line hover:border-accent/50"}`}
            >✦ Laser {t.laser ? "ON" : "off"}</button>
          </div>
        </div>
      </div>

      <div className="text-[11px] text-mut">
        Native mode: the server drives the robot directly over Agora RTM and decodes its video over RTC — no
        browser tab required. Manual joystick + eyes always work, even with autonomy off.
      </div>
    </div>
  );
}
