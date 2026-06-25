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
// IMMUTABLE hard-forbidden raw ids (P0 agent_next §2.7): movement/dock/ownership/speed-mode/avoid/actuator.
// Environment config can NEVER add these to the allowlist; they only travel via typed, ticketed commands.
const RAW_HARD_FORBIDDEN = new Set([
  RTM_DRIVE,        // 101007 movement
  RTM_DOCK,         // 103043 docking
  RTM_LOGIN,        // 101003 controller ownership
  RTM_KEEPALIVE,    // 101005 ownership heartbeat
  RTM_MOVE_MODE,    // 103011 speed/mode
  RTM_AVOID,        // 103045 avoidance
  RTM_LASER,        // 103051 actuator
]);
const RAW_ALLOW = new Set([102001, 102003].concat((process.env.AUTOBOT_RTM_RAW_ALLOW || "")
  .split(",").map((x) => parseInt(x.trim(), 10))
  .filter((n) => Number.isFinite(n) && !RAW_HARD_FORBIDDEN.has(n))));   // env can never add a forbidden id

// P0 agent_next §2.1: this sidecar process's identity, generated once at startup. Included in `ready`, state
// events, and every command_result so Python can reject responses from a REPLACED sidecar instance.
const crypto = require("crypto");
const SIDECAR_ID = (crypto.randomUUID ? crypto.randomUUID() : crypto.randomBytes(16).toString("hex"));
let acceptedProcessId = null;   // the process_instance_id Python last reconciled with (set via set_control)

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
let epoch = 0;          // transition epoch (monotonic); mirrors the Python arbiter's epoch on each transition
let activeStops = 0;    // count of E-STOP dispatches currently in flight (RESET refused while > 0)
let preparedReset = null;   // agent_next_2 §2: the ONE prepared-reset record {nonce, releaseEpoch, ...} or null

// One place to emit a correlated command_result so EVERY result carries the sidecar identity + control state
// (P0 agent_next §2). Extra fields override the defaults.
function result(c, extra) {
  out(Object.assign({
    ev: "command_result", command_id: (c && c.command_id != null) ? c.command_id : null,
    cmd: c && c.cmd, sidecar_instance_id: SIDECAR_ID, accepted_process_id: acceptedProcessId,
    latched, generation, epoch, rtm_connected: connected,
    dispatch_ts: Date.now(), completion_ts: Date.now(),
  }, extra || {}));
}

function zeroFrame() { return { lx: 0, ly: 0, rx: 0, ry: 0, buttons: 1 }; }

// agent_next_2 §4.5: validate a ticketed physical EFFECT. Returns an error string (rejected) or null (ok).
// Mandatory identity + ticket; MISSING fields are rejected, not just stale ones. STOP + zero motion bypass this.
function effectOk(c) {
  if (latched) return "estop_latched";
  if (activeStops > 0) return "estop_in_flight";
  if (c.process_instance_id == null || c.sidecar_instance_id == null) return "missing_identity";
  if (c.generation == null || c.epoch == null || c.ticket_id == null) return "missing_ticket";
  if (c.sidecar_instance_id !== SIDECAR_ID) return "wrong_sidecar_instance";
  if (acceptedProcessId != null && c.process_instance_id !== acceptedProcessId) return "wrong_process_instance";
  if (c.generation !== generation) return "stale_generation";
  if (c.epoch !== epoch) return "stale_epoch";
  return null;
}

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

// agent_next_2 §9: deterministic test seam — when armed (`__block`), the FAKE SDK send BLOCKS until `__release`,
// so a test can force "STOP initial-zero send in flight" / "priority E-STOP before a blocked send" without sleeps.
let _blockGate = null, _blockRelease = null;
async function sendRtm(id, data) {
  // Returns {ok, error}: ok is the REAL Agora sendMessageToPeer result, not a pipe write. Callers that
  // don't care (keepalive/cadence) ignore it; the acked() path forwards it as a command_result.
  if (FAKE) {
    if (_blockGate) { await _blockGate; }
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
  // Every command_result echoes the sidecar identity + authoritative latch/generation/epoch (P0 §2).
  result(c, { rtm_id: id, sent_to_agora: r.ok, error: r.error, dispatch_ts });
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
  // agent_next_2 §7.1: controller-ownership keepalive (LOGIN/KEEPALIVE) MAY run while latched — it preserves our
  // ability to issue the emergency zero-frame and is not itself motion. But avoidance-OFF is a safety-weakening
  // effect: NEVER send it while latched (prefer safety-preserving avoidance while stopped). It is sent only when
  // unlatched (the brain has its own camera motion-confirm + the robot keeps fall-arrest), like the EBO app.
  if (sess && sess.ebo_id) sendRtm(RTM_LOGIN, { userId: sess.ebo_id });
  if (!latched) sendRtm(RTM_AVOID, { avoidobstacle: false });
  timers.push(setInterval(() => sendRtm(RTM_KEEPALIVE, { state: 0 }), 2000));
  let claims = 0;
  const claimTimer = setInterval(() => {
    if (sess && sess.ebo_id) sendRtm(RTM_LOGIN, { userId: sess.ebo_id });
    if (++claims >= 5) clearInterval(claimTimer);
  }, 3000);
  timers.push(claimTimer);
  timers.push(setInterval(() => {
    if (sess && sess.ebo_id) sendRtm(RTM_LOGIN, { userId: sess.ebo_id });
    if (!latched) sendRtm(RTM_AVOID, { avoidobstacle: false });   // never weaken avoidance while latched
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
      // P0 §2.3: correlated, VALIDATED reconcile (replaces fire-and-forget). Python re-asserts the
      // authoritative {process id, epoch, generation, latch} on connect / after reconcile. Reject a stale
      // (lower) epoch/generation; never let an equal-or-lower set_control CLEAR a latch (only RESET unlatches).
      if (c.generation != null && c.generation < generation) {
        result(c, { ok: false, control_state_applied: false, control_ready: false, error: "stale_generation" }); return;
      }
      if (c.epoch != null && c.epoch < epoch) {
        result(c, { ok: false, control_state_applied: false, control_ready: false, error: "stale_epoch" }); return;
      }
      if (c.process_instance_id != null) acceptedProcessId = c.process_instance_id;
      if (c.generation != null) generation = c.generation;
      if (c.epoch != null) epoch = c.epoch;
      // agent_next_2 §1.2: set_control may ASSERT or PRESERVE a latch but must NEVER clear it. Only the explicit
      // two-phase RESET (prepare_reset -> commit_reset) may clear the latch.
      if (c.latched === true) latched = true;
      if (latched) clearDrive();
      const control_ready = FAKE || !!(inst && sess);
      result(c, { ok: true, control_state_applied: true, reconciled: true, control_ready, latch_cleared: false });
      log("info", "control reconciled (epoch " + epoch + ", gen " + generation + ", latched " + latched + ")");
      return;
    }
    case "prepare_reset": {
      // agent_next_2 §2.2: phase 1 of the two-phase release. Validate ALL fields as mandatory; on success store
      // ONE prepared-reset record + return a fresh nonce. STAY latched. This is NOT an SDK send.
      const controlReady = FAKE || !!(inst && sess);
      if (activeStops > 0) { result(c, { ok: false, prepared: false, error: "estop_in_flight" }); return; }
      if (c.process_instance_id == null || c.sidecar_instance_id == null
          || c.expected_epoch == null || c.expected_generation == null
          || c.release_epoch == null || c.release_generation == null) {
        result(c, { ok: false, prepared: false, error: "missing_identity" }); return;
      }
      if (c.sidecar_instance_id !== SIDECAR_ID
          || (acceptedProcessId != null && c.process_instance_id !== acceptedProcessId)) {
        result(c, { ok: false, prepared: false, error: "instance_mismatch" }); return;
      }
      if (c.expected_epoch !== epoch || c.expected_generation !== generation) {
        result(c, { ok: false, prepared: false, error: "stale_state" }); return;
      }
      if (!(c.release_epoch > epoch && c.release_generation > generation)) {
        result(c, { ok: false, prepared: false, error: "release_not_newer" }); return;
      }
      if (!controlReady) { result(c, { ok: false, prepared: false, control_ready: false, error: "control_not_ready" }); return; }
      if (preparedReset) { result(c, { ok: false, prepared: false, error: "reset_already_prepared" }); return; }
      const nonce = (crypto.randomUUID ? crypto.randomUUID() : crypto.randomBytes(12).toString("hex"));
      preparedReset = { nonce, releaseEpoch: c.release_epoch, releaseGeneration: c.release_generation,
                        expectedEpoch: c.expected_epoch, expectedGeneration: c.expected_generation,
                        processId: c.process_instance_id };
      // remains LATCHED; no state mutation, no SDK send
      result(c, { ok: true, prepared: true, control_state_applied: false, sdk_send_attempted: false,
                  prepare_nonce: nonce, latched: true, control_ready: controlReady });
      log("info", "RESET prepared (release epoch " + c.release_epoch + "/gen " + c.release_generation + ")");
      return;
    }
    case "commit_reset": {
      // agent_next_2 §2.3: phase 2. Commit ONLY when the same prepared reset is still active, no STOP arrived
      // after prepare, accepted state is still the expected old state, identities match, control ready. Then
      // atomically install the new release epoch/gen + clear the latch + consume the nonce. This is NOT an
      // SDK send (local state mutation only).
      const pr = preparedReset;
      if (!pr || c.prepare_nonce == null || c.prepare_nonce !== pr.nonce) {
        result(c, { ok: false, reconciled: false, error: "no_prepared_reset" }); return;
      }
      if (activeStops > 0) {                       // a STOP raced in after prepare -> invalidate
        preparedReset = null;
        result(c, { ok: false, reconciled: false, error: "estop_in_flight" }); return;
      }
      if (epoch !== pr.expectedEpoch || generation !== pr.expectedGeneration) {
        preparedReset = null;                       // state changed after prepare (e.g. a STOP) -> invalidate
        result(c, { ok: false, reconciled: false, error: "state_changed_after_prepare" }); return;
      }
      if (c.sidecar_instance_id !== SIDECAR_ID
          || (acceptedProcessId != null && c.process_instance_id !== acceptedProcessId)) {
        result(c, { ok: false, reconciled: false, error: "instance_mismatch" }); return;
      }
      const controlReady = FAKE || !!(inst && sess);
      if (!controlReady) { result(c, { ok: false, reconciled: false, control_ready: false, error: "control_not_ready" }); return; }
      // atomic commit
      epoch = pr.releaseEpoch;
      generation = pr.releaseGeneration;
      latched = false;
      preparedReset = null;
      result(c, { ok: true, reconciled: true, control_state_applied: true, sdk_send_attempted: false,
                  control_ready: true, latched: false });
      log("info", "RESET committed — motion permitted (epoch " + epoch + ", gen " + generation + ")");
      return;
    }
    case "drive": {
      // REFUSE motion while the E-STOP is latched or any STOP dispatch is in flight (correlated result so the
      // caller learns it).
      if (latched || activeStops > 0) { result(c, { sent_to_agora: false, error: latched ? "estop_latched" : "estop_in_flight" }); return; }
      // P0 §2.6/§3: a drive is a TICKETED effect — generation AND epoch are mandatory and must match the
      // accepted control state exactly (a joystick frame in flight across a STOP/RESET is stale on both axes).
      // Raw RTM 101007 cannot reach here (only `drive` does).
      if (c.generation == null) { result(c, { sent_to_agora: false, error: "missing_generation" }); return; }
      if (c.generation !== generation) { result(c, { sent_to_agora: false, error: "stale_generation" }); return; }
      if (c.epoch != null && c.epoch !== epoch) { result(c, { sent_to_agora: false, error: "stale_epoch" }); return; }
      if (c.sidecar_instance_id != null && c.sidecar_instance_id !== SIDECAR_ID) { result(c, { sent_to_agora: false, error: "wrong_sidecar_instance" }); return; }
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
    // agent_next_2 §4.5: typed physical effects REQUIRE an admitted ticket + matching identity/epoch/gen and are
    // refused while latched / mid-STOP. Missing identity or ticket is rejected (not merely stale fields).
    case "eyes": { const e = effectOk(c); if (e) { result(c, { sent_to_agora: false, error: e }); return; }
      return acked(c, RTM_EMOTE, { voiceIds: [], cycleMode: 0, emojiIds: [EYE_IDS[c.state] ?? 0], moveIds: [] }); }
    case "dock": { const e = effectOk(c); if (e) { result(c, { sent_to_agora: false, error: e }); return; }
      return acked(c, RTM_DOCK, null); }
    case "avoid": { const e = effectOk(c); if (e) { result(c, { sent_to_agora: false, error: e }); return; }
      return acked(c, RTM_AVOID, { avoidobstacle: c.on !== false }); }
    case "laser": { const e = effectOk(c); if (e) { result(c, { sent_to_agora: false, error: e }); return; }
      return acked(c, RTM_LASER, { laser: c.on !== false }); }
    case "move_mode": { const e = effectOk(c); if (e) { result(c, { sent_to_agora: false, error: e }); return; }
      return acked(c, RTM_MOVE_MODE, { moveMode: c.mode | 0 }); }
    case "move_speed": { const e = effectOk(c); if (e) { result(c, { sent_to_agora: false, error: e }); return; }
      return acked(c, RTM_MOVE_MODE, { moveSpeed: c.speed | 0 }); }
    case "release": {
      // agent_next_2 §4.3/§7: relinquishing control hands the robot to its OWN autonomy — a safety-weakening
      // effect. REFUSED while latched/mid-STOP (effectOk). Correlated result so the caller can observe it.
      const e = effectOk(c); if (e) { result(c, { sent_to_agora: false, error: e }); return; }
      clearDrive(); clearTimers();
      result(c, { control_state_applied: true, released: true, sdk_send_attempted: false });
      log("info", "control released — robot autonomy active"); return;
    }
    case "resume": {
      // Re-claim controller OWNERSHIP cadence. This is ownership (not motion) and is permitted even while
      // latched (we WANT control); it requires matching identity. Avoidance-off stays gated on !latched inside
      // the cadence, so resuming under a latch never weakens avoidance.
      if (c.process_instance_id != null && c.sidecar_instance_id != null
          && (c.sidecar_instance_id !== SIDECAR_ID
              || (acceptedProcessId != null && c.process_instance_id !== acceptedProcessId))) {
        result(c, { ok: false, error: "instance_mismatch" }); return;
      }
      startControlCadence();
      result(c, { control_state_applied: true, resumed: true, sdk_send_attempted: false });
      log("info", "control resumed"); return;
    }
    case "dock_release": {
      const e = effectOk(c); if (e) { result(c, { sent_to_agora: false, error: e }); return; }
      const r = await sendRtm(RTM_DOCK, null);
      clearDrive(); clearTimers();
      result(c, { sent_to_agora: !!r.ok, released: true, error: r.error || null });
      log("info", "dock + control released"); return;
    }
    case "__fake": { if (FAKE) _fakeFail = !!c.fail; return; }   // test-only: toggle send failure
    case "__block": { if (FAKE && !_blockGate) { _blockGate = new Promise((res) => { _blockRelease = res; }); } return; }
    case "__release": { if (FAKE && _blockRelease) { _blockRelease(); _blockGate = null; _blockRelease = null; } return; }
    case "__diag": { out({ ev: "diag", command_id: c.command_id ?? null, active_stops: activeStops,
                           prepared_reset: !!preparedReset, latched, epoch, generation,
                           sidecar_instance_id: SIDECAR_ID }); return; }
    case "raw": {
      // P0 §2.7: IMMUTABLE hard-forbidden set (movement/dock/ownership/speed/avoid/actuator) can never travel
      // raw, regardless of env. Beyond that it is an ALLOWLIST: any non-approved or unknown id is rejected.
      if (RAW_HARD_FORBIDDEN.has(c.id)) {
        result(c, { cmd: "raw", ok: false, sent_to_agora: false, error: "raw_id_hard_forbidden:" + c.id });
        log("warn", "raw id HARD-FORBIDDEN: " + c.id); return;
      }
      if (!RAW_ALLOW.has(c.id)) {
        result(c, { cmd: "raw", ok: false, sent_to_agora: false, error: "raw_id_not_allowed:" + c.id });
        log("warn", "raw id rejected: " + c.id); return;
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
  activeStops++;                                             // (0) mark a STOP dispatch in flight (RESET refused)
  try {
    latched = true;                                          // (1) latch BEFORE any await — ALWAYS, even if stale
    // agent_next_2 §5.2: a STOP NEVER lowers accepted epoch/generation. Adopt the incoming transition only when
    // it is newer; a stale STOP still latches + zeros but does not regress state. Report token current/stale.
    const inGen = (c.generation != null) ? c.generation : generation + 1;
    const inEpoch = (c.epoch != null) ? c.epoch : epoch + 1;
    const tokenStatus = (inGen > generation || inEpoch > epoch) ? "newer"
      : (inGen === generation && inEpoch === epoch) ? "current" : "stale";
    if (inGen > generation) generation = inGen;
    if (inEpoch > epoch) epoch = inEpoch;
    preparedReset = null;                                    //     a STOP invalidates any prepared RESET (§2)
    clearDrive();                                            // (3) clear repeat/timeout timers
    const z = zeroFrame();
    const dispatch_ts = Date.now();
    const r0 = await sendRtm(RTM_DRIVE, z);                  // (4) await the FIRST zero frame
    const retries = [50, 100, 200];
    for (const d of retries) setTimeout(() => sendRtm(RTM_DRIVE, z), d);   // (5) schedule retries regardless
    result(c, {                                              // honest ack: ok == transport, not local safety
      cmd: "estop", ok: !!r0.ok, local_latch_set: true, initial_zero_sdk_send_succeeded: !!r0.ok,
      sent_to_agora: !!r0.ok, retry_count: retries.length, error: r0.error || null, dispatch_ts,
      token_status: tokenStatus,                             // current | newer | stale (§5.2 — never regressed)
    });
    log("warn", "E-STOP LATCHED (epoch " + epoch + ", gen " + generation + ", initial_send=" + r0.ok + ")");
  } finally { activeStops--; }                               // (6) dispatch complete; clear in-flight
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
        result(c, { sent_to_agora: false, error: "stale_generation" });
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
// agent_next_2 §2.5 parent-death fail-safe: on stdin end / parent loss / SIGTERM, immediately LATCH, invalidate
// any prepared RESET, clear drive/effect timers, and dispatch a zero frame when transport is available — so a
// dying parent can never leave the robot unlatched or moving, and a replacement process must fully reconcile.
async function failSafeShutdown() {
  latched = true;
  preparedReset = null;
  clearDrive();
  try { await sendRtm(RTM_DRIVE, zeroFrame()); } catch (e) {}
  await teardown();
}
process.stdin.on("end", () => failSafeShutdown().finally(() => process.exit(0)));
process.on("SIGTERM", () => failSafeShutdown().finally(() => process.exit(0)));
if (FAKE) connected = true;   // test seam: report connected so command_result.rtm_connected is sane
// P0 §2.1: announce this sidecar's identity so Python binds to THIS instance and rejects a replaced one.
out({ ev: "ready", sidecar_instance_id: SIDECAR_ID });
