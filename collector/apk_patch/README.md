# apk_patch — phone credential capture (primary path)

Builds a patched Enabot app (**ROLA** and/or **EBO HOME**) that captures your robot credentials when you open
it and connect to your EBO. No root and no PC instrumentation at capture time. See
[../../docs/COLLECTOR.md](../../docs/COLLECTOR.md).

## What the patched app does

1. Loads the **Frida Gadget** at startup (via a `System.loadLibrary("gadget")` injected into the app's
   `Application` class) which auto-runs our hook (`../hooks/agent.js`).
2. The hook **finds the receiver over mDNS** (`autobot.local`) — so the SAME build works on any LAN, for any
   user, with no IP baked in — and POSTs the captured secrets to `receiver.py` on your PC.
3. We also extract the four TUTK `.so` libs into the bridge `vendor/lib/`.

## Two realities that the build must handle

- **Split bundles (XAPK):** APKPure ships ROLA/EBO HOME as an `.xapk` (a `base` APK + `config.<abi>` +
  `config.<density>` splits). You can't tap-install that, and you can't gadget-inject a split directly — it
  must be **merged into one universal APK** first.
- **ABI:** the patched app must include your phone's ABI. APKPure's default XAPK is often 32-bit
  (`armeabi-v7a`), which **64-bit-only phones reject** with a silent "App not installed". Use a build with
  `arm64-v8a` (Enabot's official direct APK is a universal `arm64-v8a`+`armeabi-v7a` build;
  `https://mediakit.enabot.com/ebo/apk-release/prod_app_google_release_latest.apk`).

## Build pipeline (all-Java; needs only a JDK + Python)

Tools used (downloaded into `build/tools/`): [`APKEditor.jar`](https://github.com/REAndroid/APKEditor)
(merge/decode/build), [`uber-apk-signer.jar`](https://github.com/patrickfav/uber-apk-signer) (zipalign +
sign), and the Frida Gadget `.so` for `arm64-v8a` and `armeabi-v7a`. This replaces the older
`objection`/`apktool` flow, which couldn't handle split bundles and needed the Android SDK.

```
# 1) (if you have an .xapk) merge splits -> one universal APK
java -jar build/tools/APKEditor.jar m -i ROLA.xapk -o build/merged.apk
#    (an official single .apk can skip this step)

# 2) decode to smali + resources
java -jar build/tools/APKEditor.jar d -i build/merged.apk -o build/decode -f

# 3) inject `System.loadLibrary("gadget")` into the app's Application <clinit>:
#       ROLA      -> com.ebo.ebocode.base.EBOApplication   (smali/classes/.../EBOApplication.smali)
#       EBO HOME  -> com.enabot.ebox.App                   (smali/classes2/.../App.smali)
#    add a <clinit> if none exists, else prepend to it (bump .locals by 1, use a fresh register).
#    Also set android:extractNativeLibs="true" in build/decode/AndroidManifest.xml.

# 4) drop the gadget + config + rendered hook into every lib/<abi>/ dir
python build/place_gadget.py build/decode            # host defaults to "auto" (mDNS)

# 5) rebuild + sign
java -jar build/tools/APKEditor.jar b -i build/decode -o build/patched-unsigned.apk -f
java -jar build/tools/uber-apk-signer.jar --apks build/patched-unsigned.apk -o build/signed
```

`build/place_gadget.py` renders `../hooks/agent.js` (substituting `__AUTOBOT_HOST__`/`__AUTOBOT_PORT__`),
writes `libhook.so` + `libgadget.config.so` (script-mode config) and copies the matching-ABI gadget as
`libgadget.so`. Pass a real IP instead of `auto` to skip mDNS and hard-code the receiver.

## Use

```bash
# 1) start the receiver on your PC (writes the top-level .env when capture completes; also serves mDNS)
python ../receiver.py --port 8400

# 2) sideload build/signed/*-debugSigned.apk on your phone (uninstall the stock app first), open it,
#    log in, and connect to your EBO once.
```

## Notes

- The patched APK is for **your** device and **your** account only. Don't distribute it.
- Some builds detect re-signed APKs (`libeboSignature.so`); if the app refuses to run after login, use the PC
  path (`../pc_frida/`) against the stock app instead.
- `build/` and any `*.apk` here are gitignored.
- `patch.py` is the older single-APK + `objection` helper; the Java pipeline above is what handles XAPKs.
