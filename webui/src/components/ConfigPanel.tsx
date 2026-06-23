import { useEffect, useState } from "react";
import type { Settings, TtsState } from "../types";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-[11px] text-mut mb-1">{label}</span>
      {children}
    </label>
  );
}

const input = "w-full bg-card2 border border-line rounded-lg px-3 py-2 text-sm";

export default function ConfigPanel({
  settings,
  tts,
  onSave,
}: {
  settings: Settings;
  tts: TtsState | null;
  onSave: (changes: Partial<Settings>) => void;
}) {
  // local draft for connection fields (saved on button); behavior saves immediately
  const [draft, setDraft] = useState({
    ai_base_url: settings.ai_base_url,
    ai_api_key: "",
    ai_model: settings.ai_model,
    ai_vision_model: settings.ai_vision_model ?? "",
  });

  useEffect(() => {
    setDraft((d) => ({
      ...d,
      ai_base_url: settings.ai_base_url,
      ai_model: settings.ai_model,
      ai_vision_model: settings.ai_vision_model ?? "",
    }));
  }, [settings.ai_base_url, settings.ai_model, settings.ai_vision_model]);

  const saveConn = () => {
    const changes: Partial<Settings> = {
      ai_base_url: draft.ai_base_url,
      ai_model: draft.ai_model,
      ai_vision_model: draft.ai_vision_model,
    };
    if (draft.ai_api_key) changes.ai_api_key = draft.ai_api_key;
    onSave(changes);
    setDraft((d) => ({ ...d, ai_api_key: "" }));
  };

  return (
    <div className="flex flex-col gap-5">
      {/* Behavior */}
      <section className="flex flex-col gap-3">
        <div className="text-[11px] uppercase tracking-wider text-mut">Behavior</div>
        <Field label="Autonomy">
          <div className="grid grid-cols-3 gap-2">
            {(["manual", "assist", "auto"] as const).map((m) => (
              <button
                key={m}
                onClick={() => onSave({ autonomy: m })}
                className={`rounded-lg py-2 text-sm border transition active:scale-95 ${
                  settings.autonomy === m ? "bg-accent border-accent" : "bg-card2 border-line"
                }`}
              >
                {m}
              </button>
            ))}
          </div>
        </Field>
        <Field label={`Max speed — ${settings.max_speed.toFixed(2)}`}>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            defaultValue={settings.max_speed}
            onChange={(e) => onSave({ max_speed: parseFloat(e.target.value) })}
            className="w-full accent-accent"
          />
        </Field>
        <Field label={`AI tick — ${settings.tick_seconds.toFixed(1)}s`}>
          <input
            type="range"
            min={1}
            max={15}
            step={0.5}
            defaultValue={settings.tick_seconds}
            onChange={(e) => onSave({ tick_seconds: parseFloat(e.target.value) })}
            className="w-full accent-accent"
          />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={settings.talk_enabled}
            onChange={(e) => onSave({ talk_enabled: e.target.checked })}
          />
          Allow the robot to talk
          {tts && !tts.available && <span className="text-warn text-xs">(TTS unavailable: {tts.backend})</span>}
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={settings.confirm_motion ?? true}
            onChange={(e) => onSave({ confirm_motion: e.target.checked })}
          />
          Confirm motion (detect stuck/blocked & escape)
        </label>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Voice engine">
            <select className={input} value={settings.tts_engine} onChange={(e) => onSave({ tts_engine: e.target.value as Settings["tts_engine"] })}>
              <option value="piper">Piper (local neural)</option>
              <option value="os">OS voice</option>
            </select>
          </Field>
          <Field label="Voice">
            <select className={input} value={settings.voice} onChange={(e) => onSave({ voice: e.target.value })}>
              <option value="">{settings.tts_engine === "piper" ? "(first available / none)" : "(default)"}</option>
              {(tts?.voices ?? []).map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </Field>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={async () => {
              try {
                const r = await fetch("/api/voice/say?text=__test__");
                if (!r.ok) return;
                const url = URL.createObjectURL(await r.blob());
                const a = new Audio(url);
                void a.play();
                a.onended = () => URL.revokeObjectURL(url);
              } catch { /* */ }
            }}
            className="bg-card2 border border-line rounded-lg py-1.5 px-3 text-sm active:scale-95"
          >
            🔊 Test voice (preview here)
          </button>
          <span className="text-xs text-mut">Plays in your browser. Robot speaker test is on the Control tab.</span>
        </div>
        {settings.tts_engine === "piper" && !(tts?.voices?.length) && (
          <div className="text-xs text-mut">No Piper voices yet. Get natural ones: <code>python scripts/get_voice.py natural</code> (or <code>female</code>/<code>male</code>/<code>jarvis</code>; <code>--list</code> for all). See docs/VOICES.md.</div>
        )}
        {tts && <div className="text-xs text-mut">TTS backend: {tts.backend}</div>}
        <Field label={`Auto-dock at battery — ${settings.autodock_pct === 0 ? "off" : settings.autodock_pct + "%"}`}>
          <input type="range" min={0} max={60} step={5} defaultValue={settings.autodock_pct}
            onChange={(e) => onSave({ autodock_pct: parseInt(e.target.value, 10) })} className="w-full accent-accent" />
        </Field>
        <Field label="Goal for the AI">
          <textarea
            rows={3}
            defaultValue={settings.goal}
            onBlur={(e) => onSave({ goal: e.target.value })}
            className={input}
          />
        </Field>
      </section>

      {/* Persona & identity */}
      <section className="flex flex-col gap-3">
        <div className="text-[11px] uppercase tracking-wider text-mut">Persona &amp; identity</div>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Name (it responds to this)">
            <input className={input} defaultValue={settings.robot_name} onBlur={(e) => onSave({ robot_name: e.target.value })} />
          </Field>
          <Field label="Owner / maker">
            <input className={input} defaultValue={settings.owner_name} onBlur={(e) => onSave({ owner_name: e.target.value })} />
          </Field>
        </div>
        <Field label="Persona">
          <textarea rows={3} defaultValue={settings.persona} onBlur={(e) => onSave({ persona: e.target.value })} className={input} />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={settings.require_name} onChange={(e) => onSave({ require_name: e.target.checked })} />
          Only respond when addressed by name
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={settings.obey_owner_only} onChange={(e) => onSave({ obey_owner_only: e.target.checked })} />
          Obey owner only (others' commands need your approval)
        </label>
      </section>

      {/* Connections */}
      <section className="flex flex-col gap-3">
        <div className="text-[11px] uppercase tracking-wider text-mut">Connections</div>
        <div className="text-xs text-mut">
          Robot link:{" "}
          <span className="text-fg font-medium">{settings.robot_link}</span>
          {settings.robot_link === "mock" && " (hardware-free dev — no robot)"}
        </div>
        <Field label="AI base URL (OpenAI-compatible)">
          <input className={input} value={draft.ai_base_url} onChange={(e) => setDraft({ ...draft, ai_base_url: e.target.value })} />
        </Field>
        <Field label="AI API key">
          <input
            type="password"
            className={input}
            placeholder={settings.ai_api_key_set ? "(set — leave blank to keep)" : "sk-…"}
            value={draft.ai_api_key}
            onChange={(e) => setDraft({ ...draft, ai_api_key: e.target.value })}
          />
        </Field>
        <Field label={settings.ai_provider === "hybrid" ? "Cortex model (the thinking LLM)" : "AI model"}>
          <input className={input} value={draft.ai_model} onChange={(e) => setDraft({ ...draft, ai_model: e.target.value })} />
        </Field>
        <Field label="Vision model (hybrid caption brain — blank if the AI model can see)">
          <input className={input} placeholder="e.g. qwen2.5vl:7b (optional)" value={draft.ai_vision_model} onChange={(e) => setDraft({ ...draft, ai_vision_model: e.target.value })} />
        </Field>
        {settings.ai_provider === "hybrid" && (
          <div className="text-xs text-mut">
            Brain: <span className="text-fg">reflex + cortex</span> — the VLM service is the eyes, the model above is the cortex.
          </div>
        )}
        <button onClick={saveConn} className="bg-accent border border-accent rounded-lg py-2 text-sm font-medium active:scale-95">
          Save connections
        </button>
      </section>
    </div>
  );
}
