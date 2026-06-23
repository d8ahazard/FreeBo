// Headless Agora RTM (1.5.1) test: can the web SDK log in + send a peer message from Node (no browser)?
// Provides minimal browser shims (WebSocket, window, navigator, location). Uses a live session dumped by
// Python to data/captures/session.json.
const fs = require("fs");
const path = require("path");

// --- browser shims ---
global.WebSocket = require("ws");
global.window = global;
global.self = global;
global.navigator = { userAgent: "Mozilla/5.0 (FreeBo headless RTM)", onLine: true, language: "en-US", platform: "node" };
global.location = { protocol: "https:", host: "localhost", href: "https://localhost/" };
global.document = { createElement: () => ({}), getElementsByTagName: () => [], addEventListener: () => {}, cookie: "" };
// Node 18+ has global fetch built in.

// The RTM SDK spins ONE Web Worker — purely a background-tab-proof timer (setInterval/clearInterval that
// posts {fakeId} back). Node timers aren't throttled, so we emulate the worker on the main thread.
global.Blob = class { constructor(parts) { this.parts = parts; } };
global.URL = global.URL || {};
global.URL.createObjectURL = () => "blob:freebo-timer";
// Minimal XMLHttpRequest backed by Node's global fetch — the RTM SDK uses XHR only for the AP lookup
// (POST protobuf to https://*.ap.sd-rtn.com/api/v1). Supports arraybuffer/text responseType.
global.XMLHttpRequest = class {
  constructor() {
    this.readyState = 0; this.status = 0; this.response = null; this.responseText = "";
    this.responseType = ""; this.withCredentials = false; this.timeout = 0; this._headers = {};
    this.onload = this.onerror = this.onreadystatechange = this.ontimeout = null;
  }
  open(method, url) { this._m = method; this._u = url; this.readyState = 1; }
  setRequestHeader(k, v) { this._headers[k] = v; }
  getAllResponseHeaders() { return ""; }
  abort() { this._aborted = true; }
  send(body) {
    const ctrl = new AbortController();
    const to = this.timeout ? setTimeout(() => ctrl.abort(), this.timeout) : null;
    fetch(this._u, { method: this._m || "GET", headers: this._headers, body: body || undefined, signal: ctrl.signal })
      .then(async (r) => {
        if (to) clearTimeout(to);
        this.status = r.status;
        if (this.responseType === "arraybuffer") this.response = await r.arrayBuffer();
        else { this.responseText = await r.text(); this.response = this.responseText; }
        this.readyState = 4;
        this.onreadystatechange && this.onreadystatechange();
        this.onload && this.onload();
      })
      .catch((e) => { if (to) clearTimeout(to); this.readyState = 4; (this.onerror || (() => {}))(e); });
  }
};
global.Worker = class {
  constructor() { this._timers = {}; this.onmessage = null; }
  postMessage(data) {
    const { name, fakeId, time } = data || {};
    if (name === "setInterval") {
      this._timers[fakeId] = setInterval(() => { this.onmessage && this.onmessage({ data: { fakeId } }); }, time);
    } else if (name === "clearInterval") {
      if (this._timers[fakeId]) { clearInterval(this._timers[fakeId]); delete this._timers[fakeId]; }
    }
  }
  terminate() { for (const id of Object.values(this._timers)) clearInterval(id); this._timers = {}; }
};

const SDK = path.resolve(__dirname, "../webui/node_modules/agora-rtm-sdk/index.js");
const AgoraRTM = require(SDK);
const sess = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../data/captures/session.json"), "utf8"));

const RTM_LOGIN = 101003, RTM_AVOID = 103045, RTM_EMOTE = 103003, RTM_DRIVE = 101007;

(async () => {
  try {
    console.log("SDK keys:", Object.keys(AgoraRTM).slice(0, 20).join(","));
    const RTM = AgoraRTM.default || AgoraRTM;
    const inst = RTM.createInstance(sess.app_id);
    inst.on("ConnectionStateChanged", (s, r) => console.log("RTM state:", s, r));
    inst.on("MessageFromPeer", (m, peer) => console.log("PEER MSG from", peer, ":", (m.text || "").slice(0, 200)));
    console.log("logging in uid=", sess.rtm.uid);
    await inst.login({ uid: String(sess.rtm.uid), token: sess.rtm.token });
    console.log("LOGIN OK");
    const send = (id, data) => inst.sendMessageToPeer(
      { text: JSON.stringify({ id, sid: sess.sid, data: data || {}, type: 0, timestamp: Date.now() }) },
      String(sess.rtm.robot_uid));
    if (sess.ebo_id) { await send(RTM_LOGIN, { userId: sess.ebo_id }); console.log("sent controller-login"); }
    await send(RTM_AVOID, { avoidobstacle: true });
    // tiny nudge: blink eyes so we can SEE it worked on the robot
    await send(RTM_EMOTE, { voiceIds: [], cycleMode: 0, emojiIds: [9], moveIds: [] });
    console.log("sent eyes(curious) + avoid");
    setTimeout(async () => { try { await inst.logout(); } catch (e) {} console.log("DONE"); process.exit(0); }, 6000);
  } catch (e) {
    console.log("RTM ERROR:", e && (e.stack || e.message || e));
    process.exit(1);
  }
})();
