/*
 * Autobot credential-capture Frida hook (shared by the phone patched-APK path and the PC adb/emulator
 * path). It hooks the TUTK calls ROLA / EBO HOME make while connecting to YOUR robot and reports the
 * captured values to the Autobot receiver on your LAN. See ../../docs/COLLECTOR.md.
 *
 * Captured: license key, UID, authKey, av identity + token, and the 0x9930 "stream-start" IOCTL blob.
 *
 * Finding the receiver (no per-user patching needed):
 *   - If a real host is baked in (__AUTOBOT_HOST__ replaced with an IP, e.g. by pc_frida/run.py), use it.
 *   - Otherwise (host left as "auto"), DISCOVER the receiver over mDNS: we query "autobot.local" (A record),
 *     which collector/receiver.py answers with the PC's LAN IP. This means ONE prebuilt APK works on any
 *     LAN — no rebaking per user/network. Captures are buffered until the receiver is discovered.
 *
 * Values are reported incrementally; the receiver assembles the full set. Nothing is printed in full
 * (masked) and nothing leaves the LAN.
 */
"use strict";

var AUTOBOT_HOST_BAKED = "__AUTOBOT_HOST__";
var AUTOBOT_PORT = parseInt("__AUTOBOT_PORT__", 10) || 8400;
var AUTOBOT_MDNS_NAME = "autobot.local"; // collector/receiver.py advertises this name over mDNS

function bakedIsReal() {
  var h = AUTOBOT_HOST_BAKED;
  return !!h && h.indexOf("AUTOBOT_HOST") < 0 && h !== "auto" && h !== "mdns" && h !== "";
}

var resolvedHost = bakedIsReal() ? AUTOBOT_HOST_BAKED : null;
var captured = {};
var sent = {};

function mask(s) {
  if (!s) return "";
  s = String(s);
  if (s.length <= 6) return "***";
  return s.slice(0, 2) + "…" + s.slice(-2);
}

// ----------------------------------------------------------------------------------------------------
// mDNS discovery (libc sockets via NativeFunction — Frida's high-level Socket API can't do the
// unconnected UDP recvfrom that a one-shot mDNS query needs).
// ----------------------------------------------------------------------------------------------------
function _sym(name) {
  try { if (typeof Module.getGlobalExportByName === "function") return Module.getGlobalExportByName(name); } catch (e) {}
  try { return Module.findExportByName(null, name); } catch (e) {}
  return null;
}
var _nfCache = {};
function _nf(name, ret, args) {
  if (_nfCache[name] !== undefined) return _nfCache[name];
  var p = _sym(name);
  _nfCache[name] = p ? new NativeFunction(p, ret, args) : null;
  return _nfCache[name];
}

function _mdnsQuery(host) {
  // Build an mDNS A-record query with the unicast-response (QU) bit set, so the responder replies
  // straight back to our ephemeral source port (no multicast group membership required).
  var labels = host.split(".");
  var b = [0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]; // id=0 flags=0 qd=1 an=0 ns=0 ar=0
  for (var i = 0; i < labels.length; i++) {
    b.push(labels[i].length & 0xff);
    for (var j = 0; j < labels[i].length; j++) b.push(labels[i].charCodeAt(j) & 0xff);
  }
  b.push(0);        // root label
  b.push(0, 1);     // QTYPE = A
  b.push(0x80, 1);  // QCLASS = IN | unicast-response bit
  return b;
}

function _skipName(bytes, off) {
  while (off < bytes.length) {
    var len = bytes[off];
    if (len === 0) return off + 1;
    if ((len & 0xc0) === 0xc0) return off + 2; // compression pointer
    off += 1 + len;
  }
  return off;
}

function _parseA(bytes) {
  if (bytes.length < 12) return null;
  var qd = (bytes[4] << 8) | bytes[5];
  var an = (bytes[6] << 8) | bytes[7];
  if (an <= 0) return null;
  var off = 12;
  for (var q = 0; q < qd; q++) { off = _skipName(bytes, off); off += 4; }
  for (var a = 0; a < an; a++) {
    off = _skipName(bytes, off);
    if (off + 10 > bytes.length) return null;
    var type = (bytes[off] << 8) | bytes[off + 1];
    var rdlen = (bytes[off + 8] << 8) | bytes[off + 9];
    var rd = off + 10;
    if (type === 1 && rdlen === 4 && rd + 4 <= bytes.length) {
      return bytes[rd] + "." + bytes[rd + 1] + "." + bytes[rd + 2] + "." + bytes[rd + 3];
    }
    off = rd + rdlen;
  }
  return null;
}

function mdnsResolve(host) {
  var socket = _nf("socket", "int", ["int", "int", "int"]);
  var sendto = _nf("sendto", "int", ["int", "pointer", "int", "int", "pointer", "int"]);
  var recvfrom = _nf("recvfrom", "int", ["int", "pointer", "int", "int", "pointer", "pointer"]);
  var closefd = _nf("close", "int", ["int"]);
  var fcntl = _nf("fcntl", "int", ["int", "int", "int"]);
  if (!socket || !sendto || !recvfrom || !closefd) return null;

  var fd = socket(2, 2, 0); // AF_INET, SOCK_DGRAM
  if (fd < 0) return null;
  try {
    if (fcntl) { var fl = fcntl(fd, 3, 0); if (fl >= 0) fcntl(fd, 4, fl | 0x800); } // O_NONBLOCK

    var dst = Memory.alloc(16);
    dst.writeU16(2);                                        // AF_INET (host byte order)
    dst.add(2).writeU8((5353 >> 8) & 0xff); dst.add(3).writeU8(5353 & 0xff); // port (net order)
    var grp = [224, 0, 0, 251];
    for (var i = 0; i < 4; i++) dst.add(4 + i).writeU8(grp[i]);
    for (i = 8; i < 16; i++) dst.add(i).writeU8(0);

    var q = _mdnsQuery(host);
    var qbuf = Memory.alloc(q.length);
    for (i = 0; i < q.length; i++) qbuf.add(i).writeU8(q[i]);
    sendto(fd, qbuf, q.length, 0, dst, 16);

    var rbuf = Memory.alloc(2048);
    var fromaddr = Memory.alloc(16);
    var fromlen = Memory.alloc(4); fromlen.writeU32(16);
    for (var tries = 0; tries < 25; tries++) { // ~0.75s worst case
      var n = recvfrom(fd, rbuf, 2048, 0, fromaddr, fromlen);
      if (n > 0) {
        var u8 = new Uint8Array(rbuf.readByteArray(n));
        var bytes = [];
        for (i = 0; i < u8.length; i++) bytes.push(u8[i]);
        var ip = _parseA(bytes);
        if (ip) return ip;
      }
      Thread.sleep(0.03);
    }
  } catch (e) {
    console.log("[autobot] mdns error: " + e);
  } finally {
    try { closefd(fd); } catch (e) {}
  }
  return null;
}

// ----------------------------------------------------------------------------------------------------
// Transport: buffer captures, flush once the receiver host is known.
// ----------------------------------------------------------------------------------------------------
function report(field, value) {
  captured[field] = value;
  console.log("[autobot] captured " + field + " = " + mask(value));
  try {
    if (typeof send === "function") send({ autobot: true, field: field, value: value }); // PC path forward
  } catch (e) {}
  flush();
}

function flush() {
  if (!resolvedHost) return;
  Object.keys(captured).forEach(function (f) {
    if (sent[f]) return;
    sent[f] = true; // optimistic; cleared on failure so the periodic loop retries
    postToReceiver(f, captured[f]).then(function (ok) { if (!ok) sent[f] = false; });
  });
}

function postToReceiver(field, value) {
  return new Promise(function (resolve) {
    try {
      var body = JSON.stringify({ field: field, value: value });
      Socket.connect({ family: "ipv4", host: resolvedHost, port: AUTOBOT_PORT })
        .then(function (conn) {
          var req =
            "POST /capture HTTP/1.1\r\n" +
            "Host: " + resolvedHost + ":" + AUTOBOT_PORT + "\r\n" +
            "Content-Type: application/json\r\n" +
            "Content-Length: " + body.length + "\r\n" +
            "Connection: close\r\n\r\n" +
            body;
          conn.output.writeAll(strToArrayBuffer(req))
            .then(function () { try { conn.close(); } catch (e) {} resolve(true); })
            .catch(function () { try { conn.close(); } catch (e) {} resolve(false); });
        })
        .catch(function (e) {
          console.log("[autobot] receiver unreachable (" + resolvedHost + ":" + AUTOBOT_PORT + "): " + e);
          resolve(false);
        });
    } catch (e) {
      console.log("[autobot] report error: " + e);
      resolve(false);
    }
  });
}

function strToArrayBuffer(str) {
  var bytes = [];
  for (var i = 0; i < str.length; i++) bytes.push(str.charCodeAt(i) & 0xff);
  return new Uint8Array(bytes).buffer;
}

function cstr(ptr) {
  try {
    return ptr.isNull() ? "" : Memory.readUtf8String(ptr);
  } catch (e) {
    return "";
  }
}

function b64(ptr, len) {
  try {
    var data = Memory.readByteArray(ptr, len);
    var bytes = new Uint8Array(data);
    var bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return base64encode(bin);
  } catch (e) {
    return "";
  }
}

var B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
function base64encode(s) {
  var out = "", i = 0;
  while (i < s.length) {
    var c1 = s.charCodeAt(i++), c2 = s.charCodeAt(i++), c3 = s.charCodeAt(i++);
    var e1 = c1 >> 2, e2 = ((c1 & 3) << 4) | (c2 >> 4);
    var e3 = isNaN(c2) ? 64 : (((c2 & 15) << 2) | (c3 >> 6));
    var e4 = isNaN(c3) ? 64 : (c3 & 63);
    out += B64.charAt(e1) + B64.charAt(e2) + (e3 === 64 ? "=" : B64.charAt(e3)) + (e4 === 64 ? "=" : B64.charAt(e4));
  }
  return out;
}

function hook(name, onEnter) {
  var p = Module.findExportByName(null, name);
  if (!p) {
    console.log("[autobot] symbol not found yet: " + name + " (will retry)");
    return false;
  }
  Interceptor.attach(p, { onEnter: onEnter });
  console.log("[autobot] hooked " + name);
  return true;
}

function install() {
  // TUTK_SDK_Set_License_Key(const char* key)
  hook("TUTK_SDK_Set_License_Key", function (args) {
    var k = cstr(args[0]);
    if (k) report("license", k);
  });

  // IOTC_Connect_ByUIDEx(const char* uid, int slot, St_IOTCConnectInput* in)
  hook("IOTC_Connect_ByUIDEx", function (args) {
    var uid = cstr(args[0]);
    if (uid) report("uid", uid);
    // authKey lives at offset 8 in St_IOTCConnectInput (see ebo_bridge.c). It's an 8-char string.
    try {
      var ak = Memory.readUtf8String(args[2].add(8));
      if (ak) report("authkey", ak);
    } catch (e) {}
  });

  // avClientStartEx(AVClientStartInConfig* in, AVClientStartOutConfig* out)
  // In the EBO config, in+16 = identity (char*), in+20 = token (char*) (see ebo_bridge.c).
  hook("avClientStartEx", function (args) {
    try {
      var idp = Memory.readPointer(args[0].add(16));
      var tkp = Memory.readPointer(args[0].add(20));
      var id = cstr(idp), tk = cstr(tkp);
      if (id) report("identity", id);
      if (tk) report("token", tk);
    } catch (e) {
      console.log("[autobot] avClientStartEx parse error: " + e);
    }
  });

  // avSendIOCtrl(int av, int ioType, const char* data, int len) — capture the 0x9930 stream-start blob.
  hook("avSendIOCtrl", function (args) {
    var io = args[1].toInt32() & 0xffff;
    if (io === 0x9930) {
      var len = args[3].toInt32();
      if (len > 0 && len <= 4096 && !captured.ioctl9930) {
        report("ioctl9930", b64(args[2], len));
      }
    }
  });

  console.log("[autobot] hooks installed. Connect to your robot in the app now.");
}

// Keep trying to discover the receiver until found; then keep flushing any unsent captures.
function ensureHost() {
  if (resolvedHost) { flush(); return; }
  var ip = mdnsResolve(AUTOBOT_MDNS_NAME);
  if (ip) {
    resolvedHost = ip;
    console.log("[autobot] receiver discovered via mDNS: " + ip + ":" + AUTOBOT_PORT);
    flush();
  }
}
if (resolvedHost) {
  console.log("[autobot] receiver host baked in: " + resolvedHost + ":" + AUTOBOT_PORT);
} else {
  console.log("[autobot] no host baked — discovering receiver via mDNS (" + AUTOBOT_MDNS_NAME + ")");
}
setInterval(ensureHost, 3000);
ensureHost();

// The TUTK .so libraries may load slightly after start; retry until the symbols appear.
var tries = 0;
var iv = setInterval(function () {
  tries++;
  if (Module.findExportByName(null, "avClientStartEx") || tries > 40) {
    clearInterval(iv);
    install();
  }
}, 500);
