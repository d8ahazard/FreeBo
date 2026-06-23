/*
 * Autobot RTM control-capture hook (appended to the credential-capture libhook.so, or loaded via frida -U).
 *
 * Goal: capture EXACTLY what the EBO Home app sends to the robot over Agora RTM — the drive/turn/dock/eyes
 * control messages — so we can correct our assumptions (e.g. that turns must be fast). Every outbound RTM
 * message (the JSON `{"id":101007,"sid":...,"data":{"lx","ly","rx","ry","buttons"},...}` and friends) is
 * logged to logcat under the AUTOBOT_RTM tag, raw, so scripts/_rtmdig.py can parse it.
 *
 * Capture:  adb logcat -c && adb logcat -s AUTOBOT_RTM:I > collector/captured/turn_sniff.txt
 *           (with the Autobot server STOPPED so the phone app is the only RTM controller — no uid conflict)
 *           Then in the app: connect -> do controlled motions (slow pivot, fast pivot, forward, arc, stop).
 *           Analyze:  python scripts/_rtmdig.py collector/captured/turn_sniff.txt
 */
"use strict";

(function () {
  function L(msg) {
    try { Java.use("android.util.Log").i("AUTOBOT_RTM", msg); } catch (e) { console.log("[AUTOBOT_RTM] " + msg); }
  }

  // A control message is a JSON string carrying an eboproto id. Log strings that look like one (so we don't
  // spam logcat with tokens/uids/etc.). We keep the FULL string so _rtmdig.py can JSON-parse it.
  function looksLikeControl(s) {
    if (typeof s !== "string" || s.length < 6 || s.length > 4000) return false;
    return s.indexOf('"id"') >= 0 || s.indexOf('"data"') >= 0 || (s[0] === "{" && s.indexOf("10") >= 0);
  }

  function logStringArgs(label, args) {
    for (var i = 0; i < args.length; i++) {
      var a = args[i];
      if (typeof a === "string" && looksLikeControl(a)) L(a);
      else if (a && typeof a === "object") {
        // Some SDKs wrap the payload in a Message object with getText()/getRawMessage().
        try { if (a.getText) { var t = a.getText(); if (looksLikeControl(t)) L(t); } } catch (e) {}
      }
    }
  }

  // Hook every overload of `method` on class `C`, logging any control-JSON string argument.
  function hookAll(C, method, label) {
    try {
      if (!C[method]) return 0;
      var ovl = C[method].overloads;
      ovl.forEach(function (m) {
        m.implementation = function () {
          try { logStringArgs(label + "." + method, arguments); } catch (e) {}
          return m.apply(this, arguments);
        };
      });
      return ovl.length;
    } catch (e) { return 0; }
  }

  // RTM SDK candidates: v2 (publish to peer/topic) and v1 (sendMessageToPeer). The Android SDK is 1.5.x but
  // the app may use either surface; hook both wherever they exist.
  var RTM_CLASSES = [
    "io.agora.rtm.RtmClient",
    "io.agora.rtm.internal.RtmClientImpl",
  ];
  var RTM_METHODS = ["publish", "publishToPeer", "sendMessageToPeer", "sendMessage", "sendMessageToChannel"];

  // Enabot wrapper: com.enabot.lib_device.agora.g builds the JSON and calls RtmClient.publish. Hooking it too
  // catches the payload even if it's transformed before the SDK call. We hook any of its methods that take a
  // String (the control JSON).
  function hookEnabotWrapper() {
    var n = 0;
    try {
      var C = Java.use("com.enabot.lib_device.agora.g");
      var ms = C.class.getDeclaredMethods();
      var seen = {};
      for (var i = 0; i < ms.length; i++) {
        var name = ms[i].getName();
        if (seen[name]) continue;
        var pts = ms[i].getParameterTypes();
        var hasStr = false;
        for (var j = 0; j < pts.length; j++) if (pts[j].getName() === "java.lang.String") hasStr = true;
        if (hasStr) { seen[name] = true; n += hookAll(C, name, "enabot.g"); }
      }
    } catch (e) {}
    return n;
  }

  var installed = false, tries = 0;
  var iv = setInterval(function () {
    if (installed) { clearInterval(iv); return; }
    tries++;
    Java.perform(function () {
      var hooked = [];
      RTM_CLASSES.forEach(function (cn) {
        var C;
        try { C = Java.use(cn); } catch (e) { return; }
        RTM_METHODS.forEach(function (m) { var k = hookAll(C, m, cn.split(".").pop()); if (k) hooked.push(m + "x" + k); });
      });
      var w = hookEnabotWrapper();
      if (w) hooked.push("enabot.g x" + w);
      if (hooked.length) {
        installed = true;
        L("agora rtm hooks installed: " + hooked.join(", "));
      } else if (tries % 20 === 0) {
        L("waiting for RTM client to load... (" + tries + "s)");
      }
    });
  }, 1000);
  L("agora rtm hook script loaded; watching for RtmClient");
})();
