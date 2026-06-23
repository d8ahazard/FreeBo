import { useState } from "react";
import { api } from "./api";
import { useAutobot } from "./hooks/useAutobot";
import ControlPanel from "./components/ControlPanel";
import NativeControlPanel from "./components/NativeControlPanel";
import ConfigPanel from "./components/ConfigPanel";
import SetupWizard from "./components/SetupWizard";
import ModeBar from "./components/ModeBar";
import AbilityToggles from "./components/AbilityToggles";
import HudTelemetry from "./components/HudTelemetry";
import SlamMap from "./components/SlamMap";
import SelfTest from "./components/SelfTest";
import ThoughtFeed from "./components/ThoughtFeed";
import MemoryPanel from "./components/MemoryPanel";
import TasksPanel from "./components/TasksPanel";
import CalibratePanel from "./components/CalibratePanel";
import VoiceCommands from "./components/VoiceCommands";

type Tab = "hud" | "memory" | "settings";

export default function App() {
  const { settings, telemetry, brain, tts, feed, identity, approvals, connected, save } = useAutobot();
  const [tab, setTab] = useState<Tab>("hud");
  const [chat, setChat] = useState("");
  const [setupDone, setSetupDone] = useState(false);
  const [logOpen, setLogOpen] = useState(true);

  const sendChat = () => {
    const t = chat.trim();
    if (!t) return;
    api.chat(t);
    setChat("");
  };

  const status = brain?.status ?? "…";
  const brainLabel =
    settings?.ai_provider === "hybrid" ? `eyes+cortex · ${settings?.ai_model ?? ""}`
    : settings?.ai_provider === "vlm" ? "VLM · whisper · piper"
    : settings?.ai_provider === "omni" ? "MiniCPM-o"
    : settings?.ai_model ?? "";
  const busy = status === "thinking" || status === "acting";
  const statusTone = status === "error" ? "bg-bad" : busy ? "bg-accent reactor" : connected ? "bg-ok" : "bg-mut";
  const showWizard = settings && !settings.setup_complete && !setupDone;

  return (
    <div className="min-h-full hud-grid relative">
      {showWizard && <SetupWizard settings={settings} onDone={() => setSetupDone(true)} />}

      {/* ── header ── */}
      <header className="sticky top-0 z-20 backdrop-blur-md bg-bg/70 border-b border-line">
        <div className="max-w-[1320px] mx-auto px-4 py-2.5 flex items-center gap-3">
          <div className={`w-3.5 h-3.5 rounded-full ${statusTone}`} />
          <h1 className="text-lg font-bold tracking-[0.25em] text-glow">FREE<span className="text-gold">BO</span></h1>
          <span className="text-[11px] hud-mono text-mut uppercase tracking-wider">
            {connected ? status : "connecting…"}{brain?.error ? ` · ${brain.error}` : ""}
          </span>
          {identity?.recognizer && identity.present.length > 0 && (
            <span className="text-[11px] text-accent hud-mono">◉ {identity.present.join(", ")}</span>
          )}
          <span className="ml-auto text-[11px] hud-mono text-mut hidden sm:inline">
            {settings ? `${settings.mode} · ${settings.autonomy}${brain?.behavior?.intent ? ` · ${brain.behavior.intent}` : ""} · ${brainLabel}` : ""}
          </span>
          <button
            onClick={() => api.estop()}
            className="bg-bad text-white font-bold rounded-lg px-4 py-2 text-sm active:scale-95 shadow-lg shadow-bad/30 border border-bad/60"
          >
            ■ STOP
          </button>
        </div>
      </header>

      {!settings ? (
        <div className="max-w-[1320px] mx-auto px-4 py-16 text-mut hud-mono">▸ booting HUD…</div>
      ) : (
        <main className="max-w-[1320px] mx-auto px-4 py-4 relative z-[1]">
          <div className="flex gap-2 mb-4 max-w-[540px]">
            {(["hud", "memory", "settings"] as Tab[]).map((tb) => (
              <button
                key={tb}
                onClick={() => setTab(tb)}
                className={`flex-1 rounded-lg py-2 text-xs uppercase tracking-[0.2em] border transition ${
                  tab === tb ? "border-accent text-accent bg-accent/10 hud-glow" : "border-line bg-card2/50 text-mut"
                }`}
              >
                {tb === "hud" ? "◎ HUD" : tb === "memory" ? "✦ Memory" : "⚙ Settings"}
              </button>
            ))}
          </div>

          {tab === "settings" ? (
            <section className="hud-panel p-4 max-w-[560px]">
              <ConfigPanel settings={settings} tts={tts} onSave={save} />
            </section>
          ) : tab === "memory" ? (
            <section className="max-w-[900px]">
              <MemoryPanel />
            </section>
          ) : (
            <div className="grid grid-cols-1 xl:grid-cols-[1fr_360px] gap-4">
              {/* left: video HUD + manual controls. Native link = server-side Agora (no browser); the
                  air2 browser-bridge link uses the in-browser Agora ControlPanel. */}
              <div className="hud-panel p-3 min-w-0">
                {settings.robot_link === "air2_native" ? (
                  <NativeControlPanel settings={settings} t={telemetry} save={save} feed={feed} />
                ) : (
                  <ControlPanel settings={settings} t={telemetry} save={save} feed={feed} />
                )}
              </div>

              {/* right: mode, abilities, telemetry, map */}
              <div className="flex flex-col gap-3">
                <ModeBar settings={settings} save={save} />
                <AbilityToggles settings={settings} save={save} />
                <HudTelemetry t={telemetry} />
                <SlamMap />
                <CalibratePanel />
                <VoiceCommands />
                <TasksPanel />
                <SelfTest />
              </div>

              {/* full-width: cognition log + chat + approvals */}
              <div className="xl:col-span-2 flex flex-col gap-3">
                {approvals.length > 0 && (
                  <section className="hud-panel border-warn/40 p-4">
                    <div className="text-[10px] uppercase tracking-[0.2em] text-warn mb-2">Awaiting your approval</div>
                    <div className="flex flex-col gap-2">
                      {approvals.map((p) => (
                        <div key={p.id} className="flex items-center gap-2 text-sm">
                          <span className="flex-1">
                            <b>{p.requester}</b> wants <code className="text-accent">{p.tool}</code>
                            <span className="text-mut"> — {p.reason}</span>
                          </span>
                          <button onClick={() => api.approve(p.id, true)} className="bg-accent/20 border border-accent rounded-lg px-3 py-1 text-xs active:scale-95">Allow</button>
                          <button onClick={() => api.approve(p.id, false)} className="bg-card2 border border-line rounded-lg px-3 py-1 text-xs active:scale-95">Deny</button>
                        </div>
                      ))}
                    </div>
                  </section>
                )}

                <section className="hud-panel p-4">
                  <div className="flex items-center justify-between mb-3">
                    <button onClick={() => setLogOpen((o) => !o)} className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow flex items-center gap-2">
                      {logOpen ? "▾" : "▸"} Cognition Log
                    </button>
                    <button
                      onClick={() => api.tick()}
                      className="text-[11px] hud-mono bg-card2/60 border border-line rounded-lg px-3 py-1.5 hover:border-accent/50 active:scale-95"
                    >
                      ▶ step
                    </button>
                  </div>
                  {logOpen && <ThoughtFeed feed={feed} />}
                  <div className="mt-3 flex gap-2">
                    <input
                      value={chat}
                      onChange={(e) => setChat(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && sendChat()}
                      placeholder={`Transmit to ${settings?.robot_name ?? "FreeBo"}…`}
                      className="flex-1 bg-bg/60 border border-line rounded-lg px-3 py-2 text-sm hud-mono focus:border-accent outline-none"
                    />
                    <button onClick={sendChat} className="bg-accent/20 border border-accent text-fg rounded-lg px-4 py-2 text-sm active:scale-95 hud-glow">
                      Send
                    </button>
                  </div>
                </section>
              </div>
            </div>
          )}
        </main>
      )}
    </div>
  );
}
