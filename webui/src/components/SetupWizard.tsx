import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { connect as webAdbConnect, webusbSupported } from "../webadb";
import type { Provider, RobotVariant, Settings } from "../types";

const input = "w-full bg-card2 border border-line rounded-lg px-3 py-2 text-sm";
const btn = "bg-accent border border-accent rounded-lg py-2 px-4 text-sm font-medium active:scale-95 disabled:opacity-50";
const btn2 = "bg-card2 border border-line rounded-lg py-2 px-4 text-sm active:scale-95";

const STEPS = ["Brain", "Enable ADB", "Capture keys", "Your robot", "Owner", "Finish"];
const VARIANTS: RobotVariant[] = ["SE", "AIR", "AIR2", "PRO", "GENERIC"];

type Dev = { serial: string; state: string; wireless: boolean };

/**
 * First-run onboarding spine. Walks the user through: pick the brain (required), enable ADB (wired USB or
 * wireless), install the capture app + snarf credentials, choose the robot model + verify the connection,
 * pair the owner, then finish. Backend orchestration lives in autobot/web/onboarding.py.
 */
export default function SetupWizard({ settings, onDone }: { settings: Settings; onDone: () => void }) {
  const [step, setStep] = useState(0);
  const [providers, setProviders] = useState<Provider[]>([]);

  // step 1 — brain
  const [providerKey, setProviderKey] = useState(settings.ai_provider || "openai");
  const [baseUrl, setBaseUrl] = useState(settings.ai_base_url);
  const [apiKey, setApiKey] = useState("");
  const [fast, setFast] = useState(settings.ai_model);
  const [heavy, setHeavy] = useState(settings.ai_summarizer_model);

  // step 2 — adb
  const [adbInfo, setAdbInfo] = useState<{ adb: boolean; devices: Dev[]; note?: string } | null>(null);
  const [hostPort, setHostPort] = useState("");
  const [pairPort, setPairPort] = useState("");
  const [pairCode, setPairCode] = useState("");
  const [adbMsg, setAdbMsg] = useState("");

  // step 3 — capture
  const [cap, setCap] = useState<any>(null);
  const capTimer = useRef<number | undefined>(undefined);

  // step 4 — robot / connect test
  const [variant, setVariant] = useState<RobotVariant>(settings.robot_variant || "SE");
  const [test, setTest] = useState<any>(null);

  // step 5 — owner
  const [owner, setOwner] = useState(settings.owner_name);
  const [enroll, setEnroll] = useState(false);
  const [ownerMsg, setOwnerMsg] = useState("");

  const [name] = useState(settings.robot_name);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.setup().then((s) => setProviders(s.providers ?? []));
  }, []);

  // poll ADB devices while on the ADB step
  useEffect(() => {
    if (step !== 1) return;
    const tick = () => api.onboardingAdb().then(setAdbInfo).catch(() => {});
    tick();
    const id = window.setInterval(tick, 2500);
    return () => window.clearInterval(id);
  }, [step]);

  // poll capture status while on the capture step
  useEffect(() => {
    if (step !== 2) return;
    const tick = () => api.captureStatus().then(setCap).catch(() => {});
    tick();
    capTimer.current = window.setInterval(tick, 2000);
    return () => window.clearInterval(capTimer.current);
  }, [step]);

  const selected = providers.find((p) => p.key === providerKey);

  const pickProvider = (key: string) => {
    setProviderKey(key);
    const p = providers.find((x) => x.key === key);
    if (p) {
      if (p.base_url) setBaseUrl(p.base_url);
      if (p.fast) setFast(p.fast);
      if (p.heavy) setHeavy(p.heavy);
    }
  };

  const saveBrain = async () => {
    const body: Record<string, unknown> = {
      ai_provider: providerKey, ai_base_url: baseUrl, ai_model: fast, ai_summarizer_model: heavy,
      robot_name: name, finish: false,
    };
    if (apiKey) body.ai_api_key = apiKey;
    await api.setupSave(body);
  };

  const doPair = async () => {
    setAdbMsg("Pairing…");
    const r = await api.onboardingPair(pairPort, pairCode);
    setAdbMsg(r.ok ? "Paired. Now connect." : `Pair failed: ${r.err || r.error || "see device"}`);
  };
  const doConnect = async () => {
    setAdbMsg("Connecting…");
    const r = await api.onboardingConnect(hostPort);
    setAdbMsg(r.ok ? "Connected over wireless ADB." : `Connect failed: ${r.err || r.error || ""}`);
  };
  const doWebUsb = async () => {
    setAdbMsg("Requesting USB device…");
    try {
      const s = await webAdbConnect();
      setAdbMsg(`USB device: ${s.info.model || s.info.serial} (Android ${s.info.android || "?"}). In-browser install is a follow-up — for now use host ADB for capture.`);
      await s.close();
    } catch (e: any) {
      setAdbMsg(`WebUSB: ${e.message || e}`);
    }
  };

  const startCapture = async () => {
    const r = await api.captureStart({});
    setCap(r);
  };
  const stopCapture = async () => {
    const r = await api.captureStop();
    setCap(r);
  };

  const runTest = async () => {
    await api.saveSettings({ robot_variant: variant });
    const r = await api.connectTest();
    setTest(r);
  };

  const saveOwner = async () => {
    setOwnerMsg("Saving…");
    const r = await api.setOwner(owner, enroll);
    setOwnerMsg(r.ok ? (r.enrolled ? "Owner set + face enrolled." : "Owner set." + (enroll ? " (face enroll skipped — recognition not available)" : "")) : `Failed: ${r.error}`);
  };

  const finish = async () => {
    setSaving(true);
    await saveBrain();
    await api.setupSave({ ai_provider: providerKey, ai_base_url: baseUrl, ai_model: fast,
      ai_summarizer_model: heavy, robot_name: name, owner_name: owner, finish: true });
    setSaving(false);
    onDone();
  };

  const next = async () => {
    if (step === 0) await saveBrain();
    setStep((s) => Math.min(s + 1, STEPS.length - 1));
  };
  const back = () => setStep((s) => Math.max(s - 1, 0));

  const capComplete = cap?.complete;
  const capturedCount = cap ? Object.values(cap.fields || {}).filter(Boolean).length : 0;
  const capTotal = cap ? Object.keys(cap.fields || {}).length : 6;

  return (
    <div className="fixed inset-0 z-30 bg-bg/95 backdrop-blur overflow-auto">
      <div className="max-w-[680px] mx-auto px-4 py-10">
        <h1 className="text-2xl font-bold mb-1">Welcome — let's bring {name || "your robot"} alive 🤖</h1>
        {/* step rail */}
        <div className="flex flex-wrap gap-1.5 my-4">
          {STEPS.map((label, i) => (
            <button
              key={label}
              onClick={() => setStep(i)}
              className={`text-xs rounded-full px-3 py-1 border transition ${
                i === step ? "bg-accent border-accent" : i < step ? "bg-card2 border-line text-mut" : "bg-card border-line text-mut"
              }`}
            >
              {i + 1}. {label}
            </button>
          ))}
        </div>

        <div className="bg-card border border-line rounded-2xl p-5 flex flex-col gap-4">
          {/* ---- step 1: brain ---- */}
          {step === 0 && (
            <>
              <div className="text-[11px] uppercase tracking-wider text-mut">Pick the brain (required)</div>
              <div className="grid grid-cols-2 gap-2">
                {providers.map((p) => (
                  <button key={p.key} onClick={() => pickProvider(p.key)}
                    className={`text-left rounded-lg p-3 border text-sm transition ${providerKey === p.key ? "bg-accent border-accent" : "bg-card2 border-line"}`}>
                    <div className="font-medium">{p.name}</div>
                  </button>
                ))}
              </div>
              {selected?.notes && <p className="text-xs text-mut">{selected.notes}</p>}
              <label className="block"><span className="block text-[11px] text-mut mb-1">Base URL</span>
                <input className={input} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} /></label>
              {selected?.needs_key !== false && (
                <label className="block"><span className="block text-[11px] text-mut mb-1">API key</span>
                  <input type="password" className={input} value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-…" /></label>
              )}
              <div className="grid grid-cols-2 gap-2">
                <label className="block"><span className="block text-[11px] text-mut mb-1">Fast model (every tick)</span>
                  <input className={input} value={fast} onChange={(e) => setFast(e.target.value)} /></label>
                <label className="block"><span className="block text-[11px] text-mut mb-1">Heavy model (daily memory)</span>
                  <input className={input} value={heavy} onChange={(e) => setHeavy(e.target.value)} /></label>
              </div>
              <p className="text-xs text-mut">The brain must be set before FreeBo can run autonomously.</p>
            </>
          )}

          {/* ---- step 2: enable ADB ---- */}
          {step === 1 && (
            <>
              <div className="text-[11px] uppercase tracking-wider text-mut">Enable ADB on your phone</div>
              <p className="text-sm text-mut">
                FreeBo captures your robot's credentials once, using a prepared app on your phone. First enable
                <b> Developer Options → USB/Wireless debugging</b>. Then connect your phone:
              </p>

              <div className="rounded-lg border border-line p-3 flex flex-col gap-2">
                <div className="text-sm font-medium">Wired (USB) — most reliable</div>
                <div className="text-xs text-mut">Plug the phone in and accept the "Allow USB debugging" prompt.</div>
                {webusbSupported() && (
                  <button className={btn2} onClick={doWebUsb}>Connect via USB in this browser (no host adb)</button>
                )}
              </div>

              <div className="rounded-lg border border-line p-3 flex flex-col gap-2">
                <div className="text-sm font-medium">Wireless (Android 11+)</div>
                <div className="grid grid-cols-[1fr_auto] gap-2 items-end">
                  <label className="block"><span className="block text-[11px] text-mut mb-1">Pair host:port (Wireless debugging → Pair device)</span>
                    <input className={input} value={pairPort} onChange={(e) => setPairPort(e.target.value)} placeholder="192.168.1.20:37123" /></label>
                  <label className="block"><span className="block text-[11px] text-mut mb-1">Pairing code</span>
                    <input className={input} value={pairCode} onChange={(e) => setPairCode(e.target.value)} placeholder="123456" /></label>
                </div>
                <button className={btn2} onClick={doPair}>Pair</button>
                <div className="grid grid-cols-[1fr_auto] gap-2 items-end">
                  <label className="block"><span className="block text-[11px] text-mut mb-1">Connect host:port (the main debugging port)</span>
                    <input className={input} value={hostPort} onChange={(e) => setHostPort(e.target.value)} placeholder="192.168.1.20:5555" /></label>
                  <button className={btn2} onClick={doConnect}>Connect</button>
                </div>
              </div>

              <div className="text-xs">
                {adbInfo && !adbInfo.adb && <div className="text-warn">{adbInfo.note}</div>}
                {adbInfo?.devices?.length ? (
                  <div className="text-ok">Detected: {adbInfo.devices.map((d) => `${d.serial} (${d.state})`).join(", ")}</div>
                ) : adbInfo?.adb ? <div className="text-mut">No devices yet…</div> : null}
                {adbMsg && <div className="text-mut mt-1">{adbMsg}</div>}
              </div>
            </>
          )}

          {/* ---- step 3: capture ---- */}
          {step === 2 && (
            <>
              <div className="text-[11px] uppercase tracking-wider text-mut">Capture robot credentials</div>
              <p className="text-sm text-mut">
                Click <b>Install &amp; start capture</b>. FreeBo installs a one-time capture app on your phone and
                launches it. Then, <b>on your phone, open the EBO app and connect to your robot once</b> — the
                keys appear below automatically. (Close the official Enabot app first if it's open.)
              </p>
              <div className="flex gap-2">
                <button className={btn} onClick={startCapture}>Install &amp; start capture</button>
                <button className={btn2} onClick={stopCapture}>Stop</button>
              </div>
              {cap?.instruction && <div className="text-xs text-accent">{cap.instruction}</div>}
              {cap?.error && <div className="text-xs text-bad">{cap.error}</div>}
              {cap?.fields && (
                <div className="rounded-lg border border-line p-3 text-sm flex flex-col gap-1">
                  <div className="flex justify-between"><span className="text-mut">Captured {capturedCount}/{capTotal}</span>
                    <span className={capComplete ? "text-ok" : "text-warn"}>
                      {capComplete ? "ready ✓" : cap.capturing ? "waiting for connect…" : ""}</span></div>
                  {Object.entries(cap.fields).map(([k, v]) => (
                    <div key={k} className="flex justify-between text-xs"><code>{k}</code><span className={v ? "text-ok" : "text-mut"}>{(v as string) || "—"}</span></div>
                  ))}
                </div>
              )}
            </>
          )}

          {/* ---- step 4: robot + connect test ---- */}
          {step === 3 && (
            <>
              <div className="text-[11px] uppercase tracking-wider text-mut">Your robot</div>
              <label className="block"><span className="block text-[11px] text-mut mb-1">Model — sets how FreeBo talks to it</span>
                <select className={input} value={variant} onChange={(e) => setVariant(e.target.value as RobotVariant)}>
                  {VARIANTS.map((v) => <option key={v} value={v}>{v}{v === "SE" ? " (LAN, fully local)" : v === "AIR2" || v === "PRO" ? " (cloud RTM — Phase C)" : ""}</option>)}
                </select></label>
              <button className={btn} onClick={runTest}>Test connection</button>
              {test && (
                <div className="rounded-lg border border-line p-3 text-sm flex flex-col gap-1">
                  <div>Link: <b>{test.robot_link}</b> · Variant: <b>{test.variant}</b></div>
                  <div>Credentials: <span className={test.creds_present ? "text-ok" : "text-warn"}>{test.creds_present ? "present" : "missing"}</span></div>
                  <div>Connected: <span className={test.connected ? "text-ok" : "text-warn"}>{test.connected ? "yes" : "no"}</span>{test.frames ? ` · ${test.frames} frames` : ""}</div>
                  {test.untested && <div className="text-warn text-xs">x86/Windows transport is UNTESTED — see docs/TODO-FREEBO.md</div>}
                  {test.hint && <div className="text-mut text-xs">{test.hint}</div>}
                </div>
              )}
            </>
          )}

          {/* ---- step 5: owner pairing ---- */}
          {step === 4 && (
            <>
              <div className="text-[11px] uppercase tracking-wider text-mut">Pair yourself as the owner</div>
              <label className="block"><span className="block text-[11px] text-mut mb-1">Your name (owner / maker)</span>
                <input className={input} value={owner} onChange={(e) => setOwner(e.target.value)} /></label>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={enroll} onChange={(e) => setEnroll(e.target.checked)} />
                Enroll my face now (look at the camera) — needs face recognition installed
              </label>
              <button className={btn} onClick={saveOwner} disabled={!owner.trim()}>Save owner</button>
              {ownerMsg && <div className="text-xs text-mut">{ownerMsg}</div>}
            </>
          )}

          {/* ---- step 6: finish ---- */}
          {step === 5 && (
            <>
              <div className="text-[11px] uppercase tracking-wider text-mut">All set</div>
              <p className="text-sm text-mut">
                The brain is configured{owner ? `, ${owner} is the owner` : ""}. Finish to open the dashboard.
                You can re-run any step from the rail above, and change everything later in Config.
              </p>
              <button className={btn} onClick={finish} disabled={saving}>{saving ? "Saving…" : "Finish setup"}</button>
            </>
          )}

          {/* nav */}
          <div className="flex gap-2 pt-2">
            {step > 0 && <button onClick={back} className={btn2}>Back</button>}
            {step < STEPS.length - 1 && <button onClick={next} className={`${btn} flex-1`}>Next</button>}
            <button onClick={finish} className="text-xs text-mut underline self-center ml-auto">Skip & finish</button>
          </div>
        </div>
      </div>
    </div>
  );
}
