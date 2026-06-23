# EBO Air 2 — cloud control (reverse-engineering findings)

Recovered live on 2026-06-20 from a connected EBO Air 2 (`robot_id 20717`, `machine_code 123AASH258P0537`,
`ebo_id 5QZU9SLP`) via the logcat-instrumented app, plus smali analysis of the EBO Home app. This documents
what it takes to control an **Air 2 / EBO Max-class** unit, which — unlike the EBO SE — is **fully cloud**:
no usable TUTK LAN control (TUTK `avClientStartEx` is closed by the device, `-20015`).

## Architecture (confirmed)

- **Control plane:** cloud **RTM** JSON messages — `{"id":<int>,"sid":"<session>","data":{...},"type":0,"timestamp":<ms>}`.
  Session `sid` from the REST session call; commands are the `eboproto` RTM ids (drive 101007, dock 103043, …).
- **Media plane:** **Agora RTC** (the app bundles `libagora_rtc_sdk` et al). Video/audio is an Agora channel,
  NOT TUTK. "Take a picture" = subscribe to the robot's Agora RTC video and grab a frame.
- **Bootstrap:** signed REST to `https://ebox-us.enabotserverintl.com` (region-specific):
  - `GET /api/v2/users/details`
  - `GET /api/v1/ebox/robots/robot` (device list)
  - `GET /api/v2/users/app_token?device_id=...` (likely the Agora/RTM token)
  - `GET /api/v1/ebox/robots/robot_members/{robot_id}`
  - `POST /api/v1/ebox/robots/session {"robot_id":20717}` ← creates the control session
  - plus `https://ecp-api.enabotserverintl.com/...` for activity.

## The `x-ebo-sign` request signature (fully recovered)

From `com.enabot.lib_ebo.netWork.ServerEncryptHelper#b(...)`:

```
nonce      = random 8 chars from [a-zA-Z0-9]
timestamp  = currentTimeMillis() / 1000           # seconds
signHeaders= TreeMap-sorted "k=v&k=v" of:
               x-ebo-sign-nonce, x-ebo-sign-timestamp, x-ebo-sign-version=2, x-ebo-app-type=2
query      = TreeMap-sorted "k=v&k=v" of URL query params ("" if none)
bodyHash   = "" if multipart else MD5hex(jsonBody)   # lib_base.utils.b.o0()
signString = METHOD + "&" + encodedPath + "&" + query + "&" + signHeaders + "&" + bodyHash
x-ebo-sign = Base64_NO_WRAP( HMAC_SHA256( signString.getBytes(UTF8), headerAccessKeySecret.getBytes(UTF8) ) )
```

Sent headers: `x-ebo-sign`, `x-ebo-sign-timestamp`, `x-ebo-sign-nonce`, `x-ebo-sign-version=2`,
`x-ebo-app-type=2`, `x-platform=Android`, `x-app-version`, `x-os-version`. (Confirmed: captured signs are
44-char Base64 = 32-byte SHA-256 output.)

### The blocker: the secret is native

`headerAccessKeySecret` is **empty in Java** and filled at runtime by a native lib:
`com.enabot.lib.signature.SignatureHelper` does `System.loadLibrary("eboSignature")` and a native
`loadSignatureResources(flavor, bool)` populates `headerAccessKeySecret` (+ `bodyEncryptKeyS2`) per build
flavor (`prod_intl` here). So the HMAC key lives in `libeboSignature.so`, not in smali.

**Capture it with Frida (one hook):** read the static field after load —
`SignatureHelper.headerAccessKeySecret` (and `bodyEncryptKeyS2`) — or hook the native setter. Once we have
that string, the signing above can be reproduced exactly in Python and the REST layer works.

## VERIFIED (2026-06-20)

- **Secret captured.** Added a `dumpSecrets()` logger to `SignatureHelper` via smali, rebuilt the app with
  `APKEditor.jar` + signed with `uber-apk-signer.jar`, reinstalled, and read `headerAccessKeySecret` (16
  chars), `bodyEncryptKeyS2`, and the captcha id from logcat. (Stored locally as `EBO_SIGN_SECRET` in `.env`.)
- **Signing reproduced byte-for-byte** against 3 live captures (2 GET + 1 POST). Body hash confirmed
  **SHA-256 hex**. Implemented + verified in `autobot/robot/ebo_cloud.py`.
- **Live server accepts our signature** — `GET /api/v2/users/details` returns HTTP 200 (not a signature
  rejection). Remaining response: `{"code":193111,"msg":"Not login"}`.

### Next gap: the user login session

The signature authenticates the *app*; the server (`code 193111 "Not login"`) still wants the logged-in
*user* session. Findings:

- `LoginUserHelper.e()` (`app_token_key` in MMKV) is the **FCM push token** (`...:APA91b...`), NOT the API
  auth — captured it, it's a dead end for auth.
- The captured API requests carry **no** `Authorization`/token header, yet succeed in-app → the session is a
  **cookie** set at login and attached by OkHttp's `CookieJar` at the network layer (below the point our
  logger sees). It lives in `com.enabot.lib_base.web.OkHttpUtils`.

To capture it: log the final outgoing request's `Cookie` header (or the CookieJar) in `OkHttpUtils`, rebuild
(the smali->APKEditor->uber-apk-signer->adb pipeline works), set it as `EBO_ACCESS_TOKEN`/cookie in
`ebo_cloud.py`. Then `robots/robot`, `robots/session`, `users/app_token` return real data — authenticated
cloud access + the control session.

### After that: the Agora media/control layer (the real build)

Even with authenticated REST + a session, **driving + video** need an **Agora RTM** client (send the
`eboproto` `{"id":...}` control JSON) and an **Agora RTC** client (subscribe to the robot's video to grab a
frame). Agora RTC is a full native real-time stack — that's the substantial remaining engineering, not a
capture step.

## What "picture + drive" requires (honest scope)

1. **Secret** — Frida-capture `headerAccessKeySecret` (blocks everything below). [capture step]
2. **Signed REST client** — reproduce the signature; log in / reuse the app session; call `robots/session`
   + `users/app_token` and read the RESPONSES (we only captured requests so far) to get the Agora app-id,
   channel, RTC/RTM token.
3. **RTM client** — send `eboproto` RTM JSON (drive/dock/etc.) on the cloud channel. Likely Agora RTM.
4. **RTC client** — subscribe to the robot's Agora RTC video to grab a frame ("take a picture"). This is the
   heavy part: Agora RTC is a full native real-time stack; there is no trivial pure-Python receiver.

Items 2–4 are a sizeable build with an external Agora SDK dependency; this is a multi-stage project, not a
quick patch. `autobot/robot/rtm.py` is the scaffold these slot into; `proto.py` already builds the RTM JSON.

## Contrast: EBO SE

The SE is fully local (LAN MAVLink over TUTK RDT) and is what FreeBo's `native`/`native_x86` links target
today. If "alive now" matters more than "this specific Air 2", an SE works end-to-end without any cloud.
