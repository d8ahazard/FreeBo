import { useState } from "react";
import { api } from "../api";
import Joystick from "./Joystick";
import type { Settings, Telemetry } from "../types";

const TOGGLES: [string, string][] = [
  ["night", "🌗 Night vision"],
  ["avoid", "🛡️ Anti-collision"],
  ["fall", "🪜 Fall protect"],
  ["patrol", "👮 Patrol"],
];

function Btn({ onClick, children, className = "" }: { onClick: () => void; children: React.ReactNode; className?: string }) {
  return (
    <button
      onClick={onClick}
      className={`bg-card2 border border-line rounded-lg px-3 py-2 text-sm hover:bg-line active:scale-95 transition ${className}`}
    >
      {children}
    </button>
  );
}

export default function ManualControls({ settings, t }: { settings: Settings; t: Telemetry }) {
  const [sayText, setSayText] = useState("");
  const eyeAnims = t.eye_animations || [];

  return (
    <div className="flex flex-col gap-4">
      <Joystick
        maxSpeed={settings.max_speed}
        onDrive={(ly, rx) => api.drive(ly, rx)}
        onStop={() => api.stop()}
        disabled={!t.connected || !t.awake}
      />

      <div className="grid grid-cols-2 gap-2">
        <Btn onClick={() => api.action("wake")}>☀️ Wake</Btn>
        <Btn onClick={() => api.action("sleep")}>🌙 Sleep</Btn>
        <Btn onClick={() => api.action("dock")}>🏠 Dock</Btn>
        <Btn onClick={() => api.action("undock")}>✋ Undock</Btn>
      </div>

      <div>
        <div className="text-[11px] uppercase tracking-wider text-mut mb-1">Eyes</div>
        <div className="flex gap-2 flex-wrap">
          {(eyeAnims.length ? eyeAnims : ["on", "off"]).map((a) => (
            <Btn key={a} onClick={() => api.action(`eyes_${a}`)} className="text-xs">
              👀 {a}
            </Btn>
          ))}
        </div>
      </div>

      <div>
        <div className="text-[11px] uppercase tracking-wider text-mut mb-1">Features</div>
        <div className="grid grid-cols-2 gap-2">
          {TOGGLES.map(([key, label]) => {
            const on = !!(t.toggles && t.toggles[key]);
            return (
              <button
                key={key}
                onClick={() => api.action(`${key}_${on ? "off" : "on"}`)}
                className={`rounded-lg px-3 py-2 text-xs border transition active:scale-95 ${
                  on ? "bg-ok/20 border-ok text-ok" : "bg-card2 border-line"
                }`}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>

      <div>
        <div className="text-[11px] uppercase tracking-wider text-mut mb-1">
          Speak {settings.talk_enabled ? "" : "(disabled — enable in Behavior)"}
        </div>
        <div className="flex gap-2">
          <input
            value={sayText}
            onChange={(e) => setSayText(e.target.value)}
            placeholder="Say something…"
            disabled={!settings.talk_enabled}
            className="flex-1 bg-card2 border border-line rounded-lg px-3 py-2 text-sm disabled:opacity-40"
          />
          <Btn
            onClick={() => {
              if (sayText.trim()) {
                api.say(sayText.trim());
                setSayText("");
              }
            }}
            className="bg-accent border-accent"
          >
            🔊
          </Btn>
        </div>
      </div>
    </div>
  );
}
