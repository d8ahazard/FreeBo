import type { AutobotEvent, Settings } from "./types";

async function jpost(path: string, body?: unknown) {
  const r = await fetch(path, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  return r.json().catch(() => ({}));
}

export const api = {
  async state() {
    const r = await fetch("/api/state");
    return r.json();
  },
  saveSettings(changes: Partial<Settings>) {
    return jpost("/api/settings", changes);
  },
  estop() {
    return jpost("/api/estop", {});
  },
  estopReset() {
    return jpost("/api/estop/reset", {});
  },
  // P0-R4.2: the explicit operator RESUME that lifts the master STOP (reconciles the sidecar first).
  resume() {
    return jpost("/api/resume", {});
  },
  sleep(on: boolean) {
    return jpost("/api/sleep", { on });
  },
  tick() {
    return jpost("/api/tick", {});
  },
  chat(text: string) {
    return jpost("/api/chat", { text });
  },
  approve(id: string, approved: boolean) {
    return jpost("/api/approve", { id, approved });
  },
  async setup() {
    const r = await fetch("/api/setup");
    return r.json();
  },
  setupSave(body: Record<string, unknown>) {
    return jpost("/api/setup/save", body);
  },
  // --- onboarding wizard (ADB provisioning + credential capture + connect test + owner pairing) ---
  async onboardingAdb() {
    const r = await fetch("/api/onboarding/adb");
    return r.json();
  },
  onboardingPair(host_port: string, code: string) {
    return jpost("/api/onboarding/pair", { host_port, code });
  },
  onboardingConnect(host_port: string) {
    return jpost("/api/onboarding/connect", { host_port });
  },
  captureStart(body: Record<string, unknown> = {}) {
    return jpost("/api/onboarding/capture/start", body);
  },
  async captureStatus() {
    const r = await fetch("/api/onboarding/capture/status");
    return r.json();
  },
  captureStop() {
    return jpost("/api/onboarding/capture/stop", {});
  },
  connectTest() {
    return jpost("/api/onboarding/connect-test", {});
  },
  setOwner(name: string, enroll: boolean) {
    return jpost("/api/onboarding/owner", { name, enroll });
  },
  summarizeMemory() {
    return jpost("/api/memory/summarize", {});
  },
  clearMemory() {
    return jpost("/api/memory/clear", {});
  },
  async memory() {
    const r = await fetch("/api/memory");
    return r.json();
  },
  forgetMemory(query: string) {
    return jpost("/api/memory/forget", { query });
  },
  async tasks() {
    const r = await fetch("/api/tasks");
    return r.json();
  },
  addTask(body: { text: string; in_seconds?: number; daily_time?: string; every_seconds?: number }) {
    return jpost("/api/tasks/add", body);
  },
  cancelTask(id: string) {
    return jpost("/api/tasks/cancel", { id });
  },
  control(body: Record<string, unknown>) {
    return jpost("/api/control", body);
  },
  move(ly: number, rx: number, duration = 0.4) {
    return this.control({ kind: "move", ly, rx, duration });
  },
  drive(ly: number, rx: number) {
    return this.control({ kind: "drive", ly, rx });
  },
  stop() {
    return this.control({ kind: "stop" });
  },
  action(name: string) {
    return this.control({ kind: "action", name });
  },
  say(text: string) {
    return this.control({ kind: "say", text });
  },
  snapshotUrl() {
    return `/api/snapshot.jpg?t=${Date.now()}`;
  },
  mjpegUrl() {
    return `/api/video/preview.mjpeg`;
  },
  async slamMap() {
    const r = await fetch("/api/slam/map");
    return r.json();
  },
  async calibrateStatus() {
    const r = await fetch("/api/calibrate");
    return r.json();
  },
  calibrate() {
    return jpost("/api/calibrate", {});
  },
  // --- audio calibration (temp Calibrate tab): reset starts an epoch, capture stops + saves the window ---
  async audioDiag() {
    const r = await fetch("/api/diag/audio");
    return r.json();
  },
  audioReset() {
    return jpost("/api/diag/audio/reset", {});
  },
  audioCapture(label: string) {
    return jpost("/api/diag/audio/capture", { label });
  },
  // --- overseer puppet mode: read the paralyzed brain's intent + live state, and drive the real robot ---
  async overseerState(since = 0) {
    const r = await fetch(`/api/overseer/state?since=${since}`);
    return r.json();
  },
  overseerAct(body: Record<string, unknown>) {
    return jpost("/api/overseer/act", body);
  },
  async selftest(opts: { move?: boolean; talk?: boolean; only?: string } = {}) {
    const q = new URLSearchParams();
    if (opts.move) q.set("move", "1");
    if (opts.talk) q.set("talk", "1");
    if (opts.only) q.set("only", opts.only);
    const r = await fetch(`/api/selftest?${q.toString()}`);
    return r.json();
  },
};

export function connectWs(onEvent: (e: AutobotEvent) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let timer: number | undefined;
  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data));
      } catch {
        /* ignore */
      }
    };
    ws.onclose = () => {
      if (!closed) timer = window.setTimeout(open, 1500);
    };
  };
  open();
  return () => {
    closed = true;
    if (timer) window.clearTimeout(timer);
    ws?.close();
  };
}
