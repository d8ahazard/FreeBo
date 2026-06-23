/*
 * Autobot talkback reverse-engineering hook (appended to the credential-capture libhook.so).
 *
 * Goal: capture EXACTLY how the EBO app publishes audio to the robot when you enable the mic / intercom,
 * so we can reproduce it on the native link. We hook the Agora RTC v2 (io.agora.rtc2) audio/publish API and
 * log every call (with ChannelMediaOptions field dumps) to logcat under the AUTOBOT_RTC tag. Combined with
 * the AUTOBOT_RTM control log, this shows the full enable-mic -> publish sequence.
 *
 * Capture: adb logcat -s AUTOBOT_RTC:I AUTOBOT_RTM:I   (do ONE clean action: enable mic + talk ~10s)
 */
"use strict";

(function () {
  function L(msg) {
    try { Java.use("android.util.Log").i("AUTOBOT_RTC", msg); } catch (e) { console.log("[AUTOBOT_RTC] " + msg); }
  }

  // Reflectively dump a data object's fields (ChannelMediaOptions, ClientRoleOptions, RtcConnection, ...).
  function dump(obj) {
    if (obj === null || obj === undefined) return "null";
    try {
      if (!obj.getClass) return "" + obj;
      var cls = obj.getClass();
      var out = [];
      while (cls && cls.getName() !== "java.lang.Object") {
        var fs = cls.getDeclaredFields();
        for (var i = 0; i < fs.length; i++) {
          try {
            fs[i].setAccessible(true);
            var v = fs[i].get(obj);
            if (v !== null && v !== undefined) out.push(fs[i].getName() + "=" + v);
          } catch (e) {}
        }
        cls = cls.getSuperclass();
      }
      return obj.getClass().getName().split(".").pop() + "{" + out.join(", ") + "}";
    } catch (e) { return "" + obj; }
  }

  function argStr(a) {
    if (a === null || a === undefined) return "null";
    if (typeof a === "object" && a.getClass) {
      var n = "";
      try { n = a.getClass().getName(); } catch (e) {}
      if (n.indexOf("io.agora") === 0 || n.indexOf("Options") >= 0 || n.indexOf("Connection") >= 0) return dump(a);
      return "" + a;
    }
    return "" + a;
  }

  function hookAll(C, method) {
    try {
      if (!C[method]) return 0;
      var ovl = C[method].overloads;
      ovl.forEach(function (m) {
        m.implementation = function () {
          var args = [];
          for (var i = 0; i < arguments.length; i++) args.push(argStr(arguments[i]));
          var rv = m.apply(this, arguments);
          L(method + "(" + args.join(", ") + ") -> " + rv);
          return rv;
        };
      });
      return ovl.length;
    } catch (e) { return 0; }
  }

  var METHODS = [
    "joinChannel", "joinChannelEx", "joinChannelWithUserAccount", "joinChannelWithUserAccountEx",
    "updateChannelMediaOptions", "updateChannelMediaOptionsEx",
    "muteLocalAudioStream", "muteLocalAudioStreamEx",
    "enableLocalAudio", "enableAudio", "disableAudio",
    "setClientRole", "setAudioProfile", "adjustRecordingSignalVolume",
    "setEnableSpeakerphone", "setDefaultAudioRoutetoSpeakerphone", "registerAudioFrameObserver",
    "leaveChannel", "leaveChannelEx",
  ];

  // The RTC engine dex/class only loads when the live view opens, which can be minutes after app launch.
  // So retry FOREVER (never give up) — stop only once hooks are installed. Heartbeat every ~20s so the
  // capture shows we're alive and waiting.
  var tries = 0, installed = false;
  var iv = setInterval(function () {
    if (installed) { clearInterval(iv); return; }
    tries++;
    Java.perform(function () {
      var C;
      try { C = Java.use("io.agora.rtc2.internal.RtcEngineImpl"); }
      catch (e) {
        if (tries % 20 === 0) L("waiting for RtcEngineImpl to load... (" + tries + "s)");
        return;
      }
      installed = true;
      var hooked = [];
      METHODS.forEach(function (m) { var n = hookAll(C, m); if (n) hooked.push(m + "x" + n); });
      L("agora rtc2 hooks installed: " + hooked.join(", "));
    });
  }, 1000);
  L("agora rtc hook script loaded; watching for RtcEngineImpl");
})();
