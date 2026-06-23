/*
 * FreeBo RTM sidecar — runs the real Agora RTM web SDK (1.5.x) HEADLESS in Node (no browser), so the Python
 * server can drive the EBO Air 2 cloud control plane natively. The web SDK only needs three browser shims
 * (a timer Worker, XMLHttpRequest, and window/navigator/location) to run under Node.
 *
 * Protocol with the Python parent (newline-delimited JSON):
 *   stdin  (parent -> sidecar):  {"cmd":"connect","session":{...}} | {"cmd":"drive","ly","rx","duration"}
 *                                {"cmd":"stop"} | {"cmd":"eyes","state"} | {"cmd":"dock"} | {"cmd":"avoid","on"}
 *                                {"cmd":"raw","id","data"} | {"cmd":"logout"} | {"cmd":"ping"}
 *   stdout (sidecar -> parent):  {"ev":"ready"} | {"ev":"state","state","reason"} | {"ev":"peer","raw","parsed"}
 *                                {"ev":"need_session"} | {"ev":"sent","id"} | {"ev":"log","level","msg"}
 *
 * The sidecar owns the control cadence the robot expects (keepalive 101005 ~2s, controller-login 101003 +
 * avoid refresh ~30s) and reconnects on drop, asking the parent for a fresh session when its token expires.
 */
const path = require("path");

// stdout is our JSON protocol channel ONLY — route the SDK's console.* chatter to stderr so it can't
// corrupt the newline-delimited JSON the Python parent parses.
for (const m of ["log", "info", "debug", "warn", "error", "trace"]) {
  console[m] = (...a) => {
    try {
      const s = a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" ");
      if (/peer raw message|Received a /i.test(s)) return;   // drop the SDK's per-message spam
      process.stderr.write("[rtm] " + s + "\n");
    } catch (e) { /* */ }
  };
}

// ---- browser shims (enough for agora-rtm-sdk to run in Node) ----
const WS = require(path.resolve(__dirname, "../webui/node_modules/ws"));
global.WebSocket = WS;
global.window = global;
global.self = global;
global.navigator = { userAgent: "Mozilla/5.0 (FreeBo headless RTM)", onLine: true, language: "en-US", platform: "node" };
global.location = { protocol: "https:", host: "localhost", href: "https://localhost/" };
global.document = { createElement: () => ({}), getElementsByTagName: () => [], addEventListener: () => {}, cookie: "" };

global.Blob = class { constructor(parts) { this.parts = parts; } };
global.URL = global.URL || {};
global.URL.createObjectURL = () => "blob:freebo-timer";
// The SDK's one Worker is just an anti-throttle interval timer; Node timers aren't throttled, so emulate it.
global.Worker = class {
  constructor() { this._t = {}; this.onmessage = null; }
  postMessage(d) {
    const { name, fakeId, time } = d || {};
    if (name === "setInterval") this._t[fakeId] = setInterval(() => this.onmessage && this.onmessage({ data: { fakeId } }), time);
    else if (name === "clearInterval" && this._t[fakeId]) { clearInterval(this._t[fakeId]); delete this._t[fakeId]; }
  }
  terminate() { for (const id of Object.values(this._t)) clearInterval(id); this._t = {}; }
};
// XHR backed by Node fetch — the SDK uses it only for the AP lookup.
global.XMLHttpRequest = class {
  constructor() { this.readyState = 0; this.status = 0; this.response = null; this.responseText = ""; this.responseType = ""; this.withCredentials = false; this.timeout = 0; this._h = {}; this.onload = this.onerror = this.onreadystatechange = this.ontimeout = null; }
  open(m, u) { this._m = m; this._u = u; this.readyState = 1; }
  setRequestHeader(k, v) { this._h[k] = v; }
  getAllResponseHeaders() { return ""; }
  abort() {}
  send(body) {
    const ctrl = new AbortController();
    const to = this.timeout ? setTimeout(() => ctrl.abort(), this.timeout) : null;
    fetch(this._u, { method: this._m || "GET", headers: this._h, body: body || undefined, signal: ctrl.signal })
      .then(async (r) => {
        if (to) clearTimeout(to);
        this.status = r.status;
        if (this.responseType === "arraybuffer") this.response = await r.arrayBuffer();
        else { this.responseText = await r.text(); this.response = this.responseText; }
        this.readyState = 4; this.onreadystatechange && this.onreadystatechange(); this.onload && this.onload();
      })
      .catch((e) => { if (to) clearTimeout(to); this.readyState = 4; (this.onerror || (() => {}))(e); });
  }
};

const AgoraRTM = require(path.resolve(__dirname, "../webui/node_modules/agora-rtm-sdk/index.js"));
const RTM = AgoraRTM.default || AgoraRTM;

// ---- eboproto RTM ids (mirror autobot/robot/proto.py + ControlPanel.tsx) ----
const RTM_LOGIN = 101003, RTM_DRIVE = 101007, RTM_KEEPALIVE = 101005, RTM_SUBSCRIBE = 101027;
const RTM_EMOTE = 103003, RTM_DOCK = 103043, RTM_AVOID = 103045;
const EYE_IDS = { neutral: 0, happy: 1, sad: 2, angry: 3, surprised: 4, sleepy: 5, love: 6, dizzy: 7, blink: 8, curious: 9, excited: 10, scared: 11, confused: 12, wink: 13, cool: 14 };

function out(obj) { try { process.stdout.write(JSON.stringify(obj) + "\n"); } catch (e) {} }
function log(level, msg) { out({ ev: "log", level, msg: String(msg).slice(0, 300) }); }

let inst = null;        // RTM instance
let sess = null;        // current session
let timers = [];        // keepalive / controller refresh
let connected = false;

function clearTimers() { for (const t of timers) clearInterval(t); timers = []; }

let _lastNeed = 0;
let _sendFails = 0;
function _needSession(why) {
  // Heavily throttled + single-flight (Python guards reconnect) so we never spiral into overlapping logins.
  if (Date.now() - _lastNeed > 12000) { _lastNeed = Date.now(); out({ ev: "need_session" }); log("warn", "re-provision: " + why); }
}
async function sendRtm(id, data) {
  if (!inst || !sess) return false;
  const msg = JSON.stringify({ id, sid: sess.sid, data: data || {}, type: 0, timestamp: Date.now() });
  try {
    await inst.sendMessageToPeer({ text: msg }, String(sess.rtm.robot_uid));
    _sendFails = 0; out({ ev: "sent", id }); return true;
  } catch (e) {
    // Only re-login after SUSTAINED failures (token genuinely expired) — not on a single transient 102,
    // which would otherwise thrash the session. Keepalive (~every 2s) drives this counter.
    _sendFails++;
    if (_sendFails >= 6) _needSession("sustained send failures (" + _sendFails + ")");
    return false;
  }
}

// The robot pushes telemetry as COMPRESSED RAW (binary) RTM messages, not text — so m.text is empty. Pull
// the bytes out of m.rawMessage and inflate them. We log the first few decoded payloads so the telemetry
// field shapes can be mapped, then go quiet (they arrive ~1-2/s).
const zlib = require("zlib");
let _peerLogN = 0;
function _decompress(buf) {
  for (const fn of [zlib.inflateSync, zlib.gunzipSync, zlib.inflateRawSync, zlib.brotliDecompressSync]) {
    try { const s = fn(buf).toString("utf8"); if (s) return s; } catch (e) { /* try next */ }
  }
  try { return buf.toString("utf8"); } catch (e) { return ""; }
}
function _msgText(m) {
  if (!m) return "";
  if (m.text) return m.text;
  const rm = m.rawMessage || m.raw || m.message;
  if (!rm) return "";
  try {
    const buf = Buffer.isBuffer(rm) ? rm : Buffer.from(rm.buffer ? rm.buffer : rm);
    const s = _decompress(buf);
    if (_peerLogN < 6) { _peerLogN++; log("info", "PEERDECODE(" + buf.length + "b): " + String(s).slice(0, 260)); }
    return s;
  } catch (e) { return ""; }
}

// Inbound robot RTM -> structured event for Python (battery/charge, sensors, drive-reject).
const _seenIds = new Set();
function parsePeer(text) {
  let j; try { j = JSON.parse(text); } catch (e) { return null; }
  const d = j && (j.data != null ? j.data : j);
  const p = { id: j && j.id };
  // Log one full sample per distinct message id so we can map ALL telemetry (IMU/TOF live on their own ids).
  if (j && j.id !== undefined && !_seenIds.has(j.id)) {
    _seenIds.add(j.id);
    log("info", "TELID " + j.id + ": " + JSON.stringify(d).slice(0, 1400));
  }
  // Air 2 status (id 101026) nests battery: data.battery = {level, percentage, chargeStatus}.
  const bat = d && d.battery;
  if (bat && typeof bat === "object") {
    if (typeof bat.percentage === "number") p.battery = bat.percentage;
    if (bat.chargeStatus !== undefined) p.charge = Number(bat.chargeStatus) > 0 ? 1 : 0;
  }
  if (p.battery === undefined) {
    const pct = d && (d.percentage ?? (typeof d.battery === "number" ? d.battery : undefined)
                      ?? d.level ?? d.electric ?? d.power);
    if (typeof pct === "number") p.battery = pct;
  }
  if (p.charge === undefined) {
    const chg = d && (d.chargeStatus ?? d.adapterStatus ?? d.charging ?? d.charge ?? d.isCharging);
    if (chg !== undefined) p.charge = (typeof chg === "boolean" ? (chg ? 1 : 0) : Number(chg) > 0 ? 1 : 0);
  }
  // Air 2 status/settings (ids 101026 / 101028): laser (IR sensor), move speed/mode, low-batt threshold.
  const stt = d && d.status;
  if (stt && typeof stt === "object") {
    if (stt.laserStatus !== undefined) p.laser = Number(stt.laserStatus) > 0 ? 1 : 0;
    if (stt.liveStatus !== undefined) p.liveStatus = stt.liveStatus;
  }
  for (const k of ["moveSpeed", "moveMode", "lowBatteryPercentage", "avoidobstacle"]) {
    if (d && d[k] !== undefined) p[k] = d[k];
  }
  // 6-axis IMU / TOF / touch — parsed if the firmware emits them on any id.
  const imu = d && (d.imu ?? d.accel ?? d.acceleration ?? d.sensor ?? d.gsensor ?? d.posture ?? d.attitude);
  if (imu && typeof imu === "object") p.imu = imu;
  const gyro = d && (d.gyro ?? d.gyroscope);
  if (gyro && typeof gyro === "object") p.gyro = gyro;
  const tof = d && (d.tof ?? d.distance ?? d.obstacleDistance ?? d.range);
  if (typeof tof === "number") p.tof = tof;
  for (const k of ["touch", "touched", "bump", "bumped", "collision"]) if (d && d[k] !== undefined) p[k] = d[k];
  const code = (j && (j.code ?? j.result)) ?? (d && (d.code ?? d.result));
  if (code === 102) p.drive_rejected = true;
  return p;
}

let _connecting = false;
async function connect(session) {
  if (_connecting) { log("warn", "connect already in progress — ignoring"); return; }
  _connecting = true;
  try {
  await teardown();
  sess = session;
  inst = RTM.createInstance(sess.app_id);
  inst.on("ConnectionStateChanged", (state, reason) => {
    connected = state === "CONNECTED";
    out({ ev: "state", state, reason });
    // ONLY a terminal ABORTED means the login is dead. "CONNECTING/LOGIN", "RECONNECTING", etc. are normal
    // and must NOT trigger a re-login (doing so spirals into overlapping instances that conflict).
    if (state === "ABORTED") _needSession("state ABORTED/" + reason);
  });
  try { inst.on("TokenExpired", () => _needSession("TokenExpired")); } catch (e) { /* */ }
  inst.on("MessageFromPeer", (m, peer) => {
    const raw = _msgText(m);
    out({ ev: "peer", peer, parsed: parsePeer(raw) });
  });
  await inst.login({ uid: String(sess.rtm.uid), token: sess.rtm.token });
  // Let the SDK settle into the CONNECTED state before the first sends — sending immediately after login()
  // resolves can fail 102 ("not logged in") for a beat.
  for (let i = 0; i < 20 && !connected; i++) await new Promise((r) => setTimeout(r, 100));
  connected = true;
  startControlCadence();
  _sendFails = 0;
  out({ ev: "connected" });
  } finally { _connecting = false; }
}

// The controller heartbeat the robot expects while WE drive it: keepalive (2s) + controller-login/avoid (30s).
// Holding this also SUPPRESSES the robot's own autonomy (e.g. low-battery return-to-dock) — release it to
// hand control back to the robot.
function startControlCadence() {
  clearTimers();
  if (sess && sess.ebo_id) sendRtm(RTM_LOGIN, { userId: sess.ebo_id });
  // avoidobstacle OFF: the EBO app sends false while driving, and ON makes the robot refuse/stall motion
  // (it blocks commanded moves on perceived obstacles). The brain has its own camera motion-confirm + the
  // robot keeps fall-arrest, so we drive with avoidance off like the app does.
  sendRtm(RTM_AVOID, { avoidobstacle: false });
  timers.push(setInterval(() => sendRtm(RTM_KEEPALIVE, { state: 0 }), 2000));
  // Claim control firmly at startup, like the EBO app (it sends controller-login ~every 3s several times
  // before driving). One login can be missed/contended; a short burst makes the grant stick.
  let claims = 0;
  const claimTimer = setInterval(() => {
    if (sess && sess.ebo_id) sendRtm(RTM_LOGIN, { userId: sess.ebo_id });
    if (++claims >= 5) clearInterval(claimTimer);
  }, 3000);
  timers.push(claimTimer);
  timers.push(setInterval(() => {
    if (sess && sess.ebo_id) sendRtm(RTM_LOGIN, { userId: sess.ebo_id });
    sendRtm(RTM_AVOID, { avoidobstacle: false });   // keep avoidance off so it never re-blocks motion
  }, 30000));
}

async function teardown() {
  clearTimers();
  clearDrive();
  try { await (inst && inst.logout && inst.logout()); } catch (e) {}
  try { if (inst && inst.removeAllListeners) inst.removeAllListeners(); } catch (e) {}
  inst = null; connected = false;
}

let driveStopTimer = null;
let driveRepeat = null;
function clearDrive() { if (driveStopTimer) { clearTimeout(driveStopTimer); driveStopTimer = null; } if (driveRepeat) { clearInterval(driveRepeat); driveRepeat = null; } }
async function handle(c) {
  switch (c.cmd) {
    case "connect": return connect(c.session);
    case "logout": return teardown();
    case "ping": return out({ ev: "pong" });
    case "drive": {
      // robot frame: forward = negative ly; scale to -100..100. The robot has a drive deadman, so a single
      // frame barely twitches — we SUSTAIN by resending the frame ~every 200ms until duration, then stop.
      const ly = -Math.round((c.ly || 0) * 100), rx = Math.round((c.rx || 0) * 100);
      clearDrive();
      const frame = { lx: 0, ly, rx, ry: 0, buttons: 0 };
      await sendRtm(RTM_DRIVE, frame);
      // Match the EBO app: it streams drive at ~10 Hz (every 100ms) while moving. 5 Hz was marginal.
      driveRepeat = setInterval(() => sendRtm(RTM_DRIVE, frame), 100);
      const dur = c.duration > 0 ? c.duration : 1.0; // never run forever without an explicit stop
      driveStopTimer = setTimeout(() => { clearDrive(); sendRtm(RTM_DRIVE, { lx: 0, ly: 0, rx: 0, ry: 0, buttons: 0 }); }, dur * 1000);
      return;
    }
    case "stop": { clearDrive(); return sendRtm(RTM_DRIVE, { lx: 0, ly: 0, rx: 0, ry: 0, buttons: 0 }); }
    case "eyes": return sendRtm(RTM_EMOTE, { voiceIds: [], cycleMode: 0, emojiIds: [EYE_IDS[c.state] ?? 0], moveIds: [] });
    case "dock": return sendRtm(RTM_DOCK, null);
    case "avoid": return sendRtm(RTM_AVOID, { avoidobstacle: c.on !== false });
    case "release": {
      // Stop our controller heartbeat so the robot reverts to its OWN autonomy (e.g. low-battery return
      // home). We stay logged into RTM (still receive telemetry) but no longer claim active control.
      clearDrive(); clearTimers(); log("info", "control released — robot autonomy active"); return;
    }
    case "resume": { startControlCadence(); log("info", "control resumed"); return; }
    case "dock_release": {
      // Tell it to dock, then immediately release control so its onboard return-to-charge can run.
      await sendRtm(RTM_DOCK, null);
      clearDrive(); clearTimers();
      log("info", "dock + control released");
      return;
    }
    case "raw": return sendRtm(c.id, c.data || {});
    default: return log("warn", "unknown cmd " + c.cmd);
  }
}

// ---- stdin command loop (newline-delimited JSON) ----
let buf = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buf += chunk;
  let nl;
  while ((nl = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
    if (!line) continue;
    let c; try { c = JSON.parse(line); } catch (e) { log("warn", "bad cmd json"); continue; }
    Promise.resolve(handle(c)).catch((e) => log("error", "cmd " + c.cmd + ": " + (e && (e.message || e))));
  }
});
process.stdin.on("end", () => teardown().finally(() => process.exit(0)));
process.on("SIGTERM", () => teardown().finally(() => process.exit(0)));
out({ ev: "ready" });
