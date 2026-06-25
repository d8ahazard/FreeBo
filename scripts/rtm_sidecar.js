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
const RTM_EMOTE = 103003, RTM_DOCK = 103043, RTM_AVOID = 103045, RTM_MOVE_MODE = 103011, RTM_LASER = 103051;
const EYE_IDS = { neutral: 0, happy: 1, sad: 2, angry: 3, surprised: 4, sleepy: 5, love: 6, dizzy: 7, blink: 8, curious: 9, excited: 10, scared: 11, confused: 12, wink: 13, cool: 14 };
// P0-R4 amendment E: the generic `raw` channel is an ALLOWLIST, not a denylist. Movement/control/dock/speed/
// avoidance and ANY unknown id are rejected and must go through typed, safety-gated commands. Approved
// diagnostic ids only (env AUTOBOT_RTM_RAW_ALLOW="id,id"); empty by default. 101007 can never be raw-sent.
// Default allow: the audio call-mode handshake ids (102001 open audio session, 102003 intercom) — non-motion
// control needed for talkback. NEVER movement/dock/speed/avoidance. Extend via env for diagnostics only.
const RAW_ALLOW = new Set([102001, 102003].concat((process.env.AUTOBOT_RTM_RAW_ALLOW || "")
  .split(",").map((x) => parseInt(x.trim(), 10)).filter((n) => Number.isFinite(n))));

function out(obj) { try { process.stdout.write(JSON.stringify(obj) + "\n"); } catch (e) {} }
function log(level, msg) { out({ ev: "log", level, msg: String(msg).slice(0, 300) }); }

let inst = null;        // RTM instance
let sess = null;        // current session
let timers = [];        // keepalive / controller refresh
let connected = false;
// P0-R4.4: default-SAFE. A freshly (re)started sidecar refuses motion until Python re-asserts the
// authoritative state via `set_control`/`estop_reset` on connect — so a restart can never silently re-enable
// movement under the wrong latch/generation.
let latched = true;     // E-STOP latch: while true, ALL drive frames are refused (zero-stop still allowed)
let generation = 0;     // control generation; bumped on E-STOP; drives stamped with a stale generation are dropped

function zeroFrame() { return { lx: 0, ly: 0, rx: 0, ry: 0, buttons: 1 }; }

function clearTimers() { for (const t of timers) clearInterval(t); timers = []; }

let _lastNeed = 0;
let _sendFails = 0;
function _needSession(why) {
  // Heavily throttled + single-flight (Python guards reconnect) so we never spiral into overlapping logins.
  if (Date.now() - _lastNeed > 12000) { _lastNeed = Date.now(); out({ ev: "need_session" }); log("warn", "re-provision: " + why); }
}
// P0-R4 item 10: test seam. With AUTOBOT_RTM_FAKE=1 the SDK send is faked (no Agora), so a child-process
// test can exercise the REAL protocol (latch/generation/queue/ack) deterministically. `_fakeFail` (env or the
// `__fake` test command) forces sends to fail, to test E-STOP initial-zero-send failure etc.
const FAKE = process.env.AUTOBOT_RTM_FAKE === "1";
let _fakeFail = process.env.AUTOBOT_RTM_FAKE_FAIL === "1";

async function sendRtm(id, data) {
  // Returns {ok, error}: ok is the REAL Agora sendMessageToPeer result, not a pipe write. Callers that
  // don't care (keepalive/cadence) ignore it; the acked() path forwards it as a command_result.
  if (FAKE) {
    if (_fakeFail) return { ok: false, error: "fake_send_failed" };
    out({ ev: "sent", id, fake: true }); return { ok: true, error: null };
  }
  if (!inst || !sess) return { ok: false, error: "not_connected" };
  const msg = JSON.stringify({ id, sid: sess.sid, data: data || {}, type: 0, timestamp: Date.now() });
  try {
    await inst.sendMessageToPeer({ text: msg }, String(sess.rtm.robot_uid));
    _sendFails = 0; out({ ev: "sent", id }); return { ok: true, error: null };
  } catch (e) {
    // Only re-login after SUSTAINED failures (token genuinely expired) — not on a single transient 102,
    // which would otherwise thrash the session. Keepalive (~every 2s) drives this counter.
    _sendFails++;
    if (_sendFails >= 6) _needSession("sustained send failures (" + _sendFails + ")");
    return { ok: false, error: String((e && (e.message || e.code || e)) || "send_failed").slice(0, 200) };
  }
}

// Send one RTM message and report a correlated command_result back to Python, so Air2NativeLink can return
// ACTUAL Agora delivery (not stdin-write success). Used for the operator/AI-visible commands.
async function acked(c, id, data) {
  const dispatch_ts = Date.now();
  const r = await sendRtm(id, data);
  // Every command_result echoes the sidecar's authoritative latch+generation so Python can reconcile (P0-R4.4).
  out({ ev: "command_result", command_id: (c && c.command_id != null) ? c.command_id : null, cmd: c && c.cmd,
        rtm_id: id, sent_to_agora: r.ok, error: r.error, dispatch_ts, completion_ts: Date.now(),
        rtm_connected: connected, latched, generation });
  return r.ok;
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
  // Periodic health stat for /api/debug/rtm (consecutive send failures + connection state).
  timers.push(setInterval(() => out({ ev: "stat", consecutive_send_failures: _sendFails, rtm_connected: connected }), 2000));
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
    case "set_control": {
      // P0-R4.4: Python re-asserts the authoritative latch + generation (on connect / after reconcile).
      if (c.generation != null) generation = c.generation;
      if (c.latched != null) latched = !!c.latched;
      if (latched) clearDrive();
      out({ ev: "command_result", command_id: (c && c.command_id != null) ? c.command_id : null, cmd: "set_control",
            sent_to_agora: true, error: null, dispatch_ts: Date.now(), completion_ts: Date.now(),
            rtm_connected: connected, latched, generation });
      log("info", "control reconciled (gen " + generation + ", latched " + latched + ")");
      return;
    }
    case "estop_reset": {
      // P0-R4 amendment B: a RESET must NOT clear a NEWER STOP. The reset captures the generation it expects;
      // if a later STOP advanced the generation, reject the reset and STAY latched (monotonic by generation).
      if (c.expected_generation != null && c.expected_generation !== generation) {
        out({ ev: "command_result", command_id: (c.command_id != null) ? c.command_id : null, cmd: "estop_reset",
              ok: false, sent_to_agora: false, error: "stale_reset_generation",
              dispatch_ts: Date.now(), completion_ts: Date.now(), rtm_connected: connected, latched, generation });
        log("warn", "RESET rejected (stale gen " + c.expected_generation + " != " + generation + ")");
        return;
      }
      latched = false;
      if (c.generation != null) generation = c.generation;
      out({ ev: "command_result", command_id: (c.command_id != null) ? c.command_id : null, cmd: "estop_reset",
            ok: true, sent_to_agora: true, error: null, dispatch_ts: Date.now(), completion_ts: Date.now(),
            rtm_connected: connected, control_ready: !!(inst && sess), latched, generation });
      log("info", "E-STOP reset — motion permitted again (gen " + generation + ")");
      return;
    }
    case "drive": {
      // REFUSE motion while the E-STOP is latched (still report a correlated result so the caller learns it).
      if (latched) {
        out({ ev: "command_result", command_id: (c && c.command_id != null) ? c.command_id : null, cmd: "drive",
              sent_to_agora: false, error: "estop_latched", dispatch_ts: Date.now(), completion_ts: Date.now(),
              rtm_connected: connected, latched, generation });
        return;
      }
      // P0-R4 amendment B/3: generation is MANDATORY and equality is required. A drive with no generation, or
      // one stamped with a generation other than the current one (e.g. a joystick frame in flight across a
      // STOP/RESET), is rejected. Raw RTM 101007 cannot reach here (only `drive` does).
      if (c.generation == null) {
        out({ ev: "command_result", command_id: (c && c.command_id != null) ? c.command_id : null, cmd: "drive",
              sent_to_agora: false, error: "missing_generation", dispatch_ts: Date.now(), completion_ts: Date.now(),
              rtm_connected: connected, latched, generation });
        return;
      }
      if (c.generation !== generation) {
        out({ ev: "command_result", command_id: (c && c.command_id != null) ? c.command_id : null, cmd: "drive",
              sent_to_agora: false, error: "stale_generation", dispatch_ts: Date.now(), completion_ts: Date.now(),
              rtm_connected: connected, latched, generation });
        return;
      }
      // robot frame: forward = negative ly; scale to -100..100. The robot has a drive deadman, so a single
      // frame barely twitches — we SUSTAIN by resending the frame ~every 200ms until duration, then stop.
      const myGen = generation;
      const ly = -Math.round((c.ly || 0) * 100), rx = Math.round((c.rx || 0) * 100);
      clearDrive();
      // buttons:1 on EVERY frame (incl. the zero/stop frame) — confirmed by sniffing the EBO Home app: it is
      // the "controller actively engaged" flag and the robot ignores joystick frames sent with buttons:0.
      const frame = { lx: 0, ly, rx, ry: 0, buttons: 1 };
      const ok0 = await acked(c, RTM_DRIVE, frame);   // ack the INITIAL frame (real Agora delivery)
      // P0-R4.4: if the INITIAL frame failed to send, do NOT start the 10 Hz repeat (don't sustain a stream
      // the robot never received). A re-check of latch/generation also guards a STOP that raced the ack.
      if (!ok0 || latched || myGen !== generation) { clearDrive(); return; }
      // Match the EBO app: it streams drive at ~10 Hz (every 100ms) while moving. 5 Hz was marginal. Each
      // repeat re-checks the latch + generation, so an E-STOP or a newer command kills this stale stream.
      driveRepeat = setInterval(() => {
        if (latched || myGen !== generation) { clearDrive(); return; }
        sendRtm(RTM_DRIVE, frame);
      }, 100);
      const dur = c.duration > 0 ? c.duration : 1.0; // never run forever without an explicit stop
      driveStopTimer = setTimeout(() => { clearDrive(); sendRtm(RTM_DRIVE, zeroFrame()); }, dur * 1000);
      return;
    }
    case "stop": { clearDrive(); return acked(c, RTM_DRIVE, zeroFrame()); }
    case "eyes": return acked(c, RTM_EMOTE, { voiceIds: [], cycleMode: 0, emojiIds: [EYE_IDS[c.state] ?? 0], moveIds: [] });
    case "dock": return acked(c, RTM_DOCK, null);
    case "avoid": return acked(c, RTM_AVOID, { avoidobstacle: c.on !== false });
    // P0-R4 amendment E: typed control commands (so these never need the generic raw channel).
    case "laser": return acked(c, RTM_LASER, { laser: c.on !== false });
    case "move_mode": return acked(c, RTM_MOVE_MODE, { moveMode: c.mode | 0 });
    case "move_speed": return acked(c, RTM_MOVE_MODE, { moveSpeed: c.speed | 0 });
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
    case "__fake": { if (FAKE) _fakeFail = !!c.fail; return; }   // test-only: toggle send failure
    case "raw": {
      // P0-R4 amendment E: allowlist only. Movement (101007) + any non-approved id is rejected and must use a
      // typed command. Unknown ids are denied by default.
      if (c.id === RTM_DRIVE || !RAW_ALLOW.has(c.id)) {
        out({ ev: "command_result", command_id: (c.command_id != null) ? c.command_id : null, cmd: "raw",
              ok: false, sent_to_agora: false, error: "raw_id_not_allowed:" + c.id,
              dispatch_ts: Date.now(), completion_ts: Date.now(), rtm_connected: connected, latched, generation });
        log("warn", "raw id rejected: " + c.id);
        return;
      }
      return c.command_id != null ? acked(c, c.id, c.data || {}) : sendRtm(c.id, c.data || {});
    }
    default: return log("warn", "unknown cmd " + c.cmd);
  }
}

// P0-R4 amendment C + item 2: E-STOP runs SYNCHRONOUSLY with priority — it never waits behind the serialized
// command queue. It latches, adopts/advances the generation (invalidating queued + active drive work via the
// generation check), clears timers, then dispatches the zero-frame burst and reports an HONEST ack:
// local_latch_set is always true; initial_zero_sdk_send_succeeded reflects the REAL first sendRtm result.
async function doEstop(c) {
  latched = true;                                            // (1) latch BEFORE any await
  generation = (c.generation != null) ? c.generation : generation + 1;  // (2) invalidate generation
  clearDrive();                                              // (3) clear repeat/timeout timers
  const z = zeroFrame();
  const dispatch_ts = Date.now();
  const r0 = await sendRtm(RTM_DRIVE, z);                    // (4) await the FIRST zero frame
  const retries = [50, 100, 200];
  for (const d of retries) setTimeout(() => sendRtm(RTM_DRIVE, z), d);   // (5) schedule retries regardless
  out({ ev: "command_result", command_id: (c.command_id != null) ? c.command_id : null, cmd: "estop",
        ok: !!r0.ok,                                         // ok == transport (NOT local safety)
        local_latch_set: true,
        initial_zero_sdk_send_succeeded: !!r0.ok,
        sent_to_agora: !!r0.ok,                              // back-compat alias = the real send result
        retry_count: retries.length,
        error: r0.error || null,
        dispatch_ts, completion_ts: Date.now(), rtm_connected: connected, latched, generation });
  log("warn", "E-STOP LATCHED (gen " + generation + ", initial_send=" + r0.ok + ")");
}

// ---- stdin command loop (newline-delimited JSON) ----
// P0-R4 amendment C: state-changing commands run through a SERIALIZED queue (one at a time) so connect /
// set_control / drive / reset never mutate latch/generation concurrently. E-STOP is the exception — it runs
// immediately, ahead of the queue. Queued drives from an older generation are rejected when finally examined.
const _queue = [];
let _pumping = false;
async function _pump() {
  if (_pumping) return;
  _pumping = true;
  try {
    while (_queue.length) {
      const c = _queue.shift();
      if (c.cmd === "drive" && c.generation != null && c.generation !== generation) {
        out({ ev: "command_result", command_id: (c.command_id != null) ? c.command_id : null, cmd: "drive",
              sent_to_agora: false, error: "stale_generation", dispatch_ts: Date.now(), completion_ts: Date.now(),
              rtm_connected: connected, latched, generation });
        continue;
      }
      try { await handle(c); } catch (e) { log("error", "cmd " + c.cmd + ": " + (e && (e.message || e))); }
    }
  } finally { _pumping = false; }
}

let buf = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buf += chunk;
  let nl;
  while ((nl = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
    if (!line) continue;
    let c; try { c = JSON.parse(line); } catch (e) { log("warn", "bad cmd json"); continue; }
    if (c.cmd === "estop") {
      // Priority: do not wait behind the queue. Latch synchronously, then dispatch.
      Promise.resolve(doEstop(c)).catch((e) => log("error", "estop: " + (e && (e.message || e))));
    } else {
      _queue.push(c);
      _pump();
    }
  }
});
process.stdin.on("end", () => teardown().finally(() => process.exit(0)));
process.on("SIGTERM", () => teardown().finally(() => process.exit(0)));
if (FAKE) connected = true;   // test seam: report connected so command_result.rtm_connected is sane
out({ ev: "ready" });
