# Credential collector

The app needs device-specific secrets that only exist while the Enabot/ROLA app talks to your robot. Autobot
provides two ways to capture them, both using the same Frida hook script. The bionic runtime is bundled
(AOSP, Apache-2.0), and the TUTK `.so` libraries are extracted from your own ROLA APK.

> You must own the robot and be licensed to use the ROLA app. The collector instruments **your** app on
> **your** device to read **your** credentials. It transmits them only to your own machine on your LAN.

## What gets captured

| value | hooked from |
|-------|-------------|
| `EBO_LICENSE` | `TUTK_SDK_Set_License_Key(key)` argument |
| `EBO_UID` | `IOTC_Connect_ByUIDEx(uid, …)` argument |
| `EBO_AUTHKEY` | `St_IOTCConnectInput.authKey` (8 bytes) |
| `EBO_IDENTITY`, `EBO_TOKEN` | `avClientStartEx` InConfig account + token |
| `ioctl9930.bin` | the `avSendIOCtrl(av, 0x9930, data, len)` payload |
| TUTK `.so` ×4 | unzipped from `lib/armeabi-v7a/` of the ROLA APK |

The hooks mask values in logs and POST the full set to `receiver.py`, which writes the top-level `.env` +
`vendor/ioctl9930.bin`.

## Which app(s) to patch

The TUTK calls happen in whichever Enabot app actually connects to your robot:

- **ROLA** (`com.enabot.ebo.intl`) — EBO SE / EBO Air / ROLA Pet*.
- **EBO HOME** (`com.enabot.ebox.intl`) — EBO X / EBO Air 2. ROLA can delegate device *binding* to EBO HOME
  ("Please bind to EBO Home app!"), so in practice you often need **both** patched.

Patch both the same way — the hook is identical and self-discovers the receiver, so one build is reusable.

## Receiver auto-discovery (mDNS) — no per-user IP baking

The hook no longer needs your PC's IP hard-coded. `collector/receiver.py` advertises itself over mDNS as
**`autobot.local`** (pure stdlib, no `zeroconf`), and `hooks/agent.js` resolves that at runtime (a one-shot
mDNS A query with the unicast-response bit, so the phone needs no multicast membership). Captures are buffered
until the receiver is found. Net effect: **one prebuilt APK works on any LAN** (phone + PC on the same
subnet). The PC/emulator path can still bake a fixed IP (`pc_frida/run.py`).

## Path A — phone (primary): patched APK

No root, no PC tooling on the phone.

1. Build patched APK(s): inject Frida Gadget + `hooks/agent.js`, set `extractNativeLibs=true` (so the
   gadget/config/script get unpacked on install), re-sign. The robust pipeline handles APKPure split
   **XAPK**s and 64-bit-only phones — see `collector/apk_patch/README.md`. Outputs e.g.
   `build/rola-autobot-arm64.apk` and `build/ebohome-autobot-arm64.apk`; the four TUTK `.so` libs are also
   extracted into the bridge `vendor/lib/`.
2. Start the receiver on the PC: `python collector/receiver.py` (HTTP `:8400` + mDNS `autobot.local`).
3. Sideload the patched APK (enable "install unknown apps"). **Uninstall the stock app first** if present — a
   re-signed APK can't replace a Play-Store-signed one (silent "App not installed"). Open it, log in, connect
   to your robot once.
4. The hook discovers the receiver via mDNS and POSTs the values; the receiver writes the top-level `.env`
   and `vendor/ioctl9930.bin`, masking values as it confirms each.

ABI note: the patched app must match your phone's ABI. APKPure's default XAPK is often 32-bit (`armeabi-v7a`),
which 64-bit-only phones reject (silent "App not installed"); prefer a build that includes `arm64-v8a`
(Enabot's official direct APK, or the APKPure arm64 XAPK).

## Path B — PC (fallback): Frida via emulator or USB phone

Easier to debug; needs a Frida-capable target.

1. Connect a rooted/USB Android phone (`adb devices`) or run an Android emulator with ROLA installed and a
   `frida-server` running on it.
2. Start the receiver: `python collector/receiver.py`.
3. Run: `python collector/pc_frida/run.py --target rola --host 127.0.0.1` — it attaches Frida with
   `hooks/agent.js`, you connect to the robot in the app, and the values POST to the receiver.

See `collector/pc_frida/README.md` for installing `frida` / `frida-server`.

## Bionic runtime (automatic)

`collector/bionic/` ships a known-good 32-bit AOSP runtime (`linker`, `libc.so`, `libm.so`, `libdl.so`,
`libc++.so`, `liblog.so`, `libstdc++.so`, `libnetd_client.so`). Copy it to `vendor/bionic/` (the `Dockerfile`
copies that into the image). You normally don't touch this. To use your own device's runtime instead, drop
replacements in `vendor/bionic/`. (This repo ships placeholders + a fetch script, not the binaries themselves.)

## Manual fallback

If automation fails on your setup, `collector/hooks/agent.js` is a standalone Frida script you can run
yourself (`frida -U -f <rola.pkg> -l collector/hooks/agent.js`) and copy the printed values into
the top-level `.env`. The format matches `.env.example`.

## Security

- Secrets are written only to the top-level `.env` and `vendor/` (gitignored).
- The receiver binds to your LAN and accepts one capture; close it when done.
- Never share `.env`, `ioctl9930.bin`, or your TUTK `.so` files.
