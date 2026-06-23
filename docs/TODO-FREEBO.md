# FreeBo — deferred work (earmarks)

Tracking the things intentionally NOT done in the initial FreeBo pass, so they aren't lost. None of these
block clone-and-run; they're follow-ups.

## 1. Deep rename (currently branding-only)

The user-facing name is **FreeBo**, but the internals still say `autobot` to avoid a high-risk churn while
other work is in flight. When ready, do the full rename:

- Python package `autobot/` -> `freebo/` (relative imports survive; fix the one dynamic import in
  `autobot/brain/skills/registry.py` and the `python -m autobot` entrypoint).
- Env vars `AUTOBOT_*` -> `FREEBO_*`, keeping `AUTOBOT_*` as **back-compat aliases** for one release.
- Infra: `/opt/autobot`, systemd `autobot.service`, pi-gen `IMG_NAME`/`TARGET_HOSTNAME`/`FIRST_USER_NAME`,
  comitup AP name `Autobot-<nnnn>`, mDNS `autobot.local`.
- Docs/rules deep pass (the `.cursor/rules/*` and `docs/*` still use `autobot/` paths — correct today).

Highest-risk items: mDNS hostname and the collector APK (both depend on `autobot.local`); coordinate with
the collector work before changing the network identity.

## 2. New collector workflow (owned by the other agent — do not edit collector/ yet)

The onboarding wizard (section 8) orchestrates a contract the collector side must implement:

- Wireless + wired (USB) ADB provisioning is driven by the backend (`autobot/web/adb.py`).
- The patched ROLA app emits creds over loopback for a **~5-minute window on start, or until a stop flag**;
  the wizard sends `stop` once all fields are captured. The patched app is ephemeral (user restores the
  official app afterward).
- Backend orchestration lives in `autobot/web/onboarding.py` and calls the collector receiver's
  `status()` / `stop()` contract. The collector's APK-side window/stop behavior and the receiver token
  handling are **the other agent's** to implement; wire them to the contract here.
- **RISK:** verify restoring the official app does not rotate `EBO_IDENTITY`/`EBO_TOKEN`
  (avClientStartEx DTLS-PSK material). Static creds (`EBO_LICENSE`/`EBO_UID`/`EBO_AUTHKEY`) should persist.
  Capture should be the last step before first connect. Confirm on the Windows/hardware test pass.

## 3. x86 / Windows transport (UNTESTED) + sdk_x86 reconciliation

`autobot/robot/x86_link.py` mirrors the proven connect/recv/ioctl flow from
`ebo-se-lan-bridge/vendor/lib/_re/sdk_x86/{tutk.py,ebo_test.py}` but has not been run end-to-end (no real
keys/library on the dev box). When keys + a real TUTK x86/Windows build land in `vendor/lib`:

- Run the Windows smoke test (license -> IOTC connect -> avClientStartEx -> first video/status frame ->
  drive/dock) and fix the struct/ABI details against the actual library.
- Reconcile `x86_link.py` with `sdk_x86` once that experimental code stabilizes (single source of truth).
- Decide x86 video streaming: today `snapshot()` decodes buffered frames via ffmpeg, but WHEP/HLS streaming
  (mediamtx pipeline like the native link) is not wired for `native_x86` yet.

## 4. eboproto into the native bridge

`autobot/robot/native/ebo_bridge.c` still uses its own hand-rolled framing. Compiling the vendored
`eboproto` into it (replacing that framing) is a safe future cleanup — `frames.py` already emits identical
bytes, so the running bridge is unaffected. Keep the vendored copy in sync with
`ebo-se-lan-bridge/vendor/lib/_re/eboproto/` and let `scripts/eboproto_check.py` guard byte-identity.

## 5. Cloud RTM transport (Phase C — Air 2 / EBO Max)

`proto.py` builds the RTM JSON commands and `ebo_route()` knows AIR2/PRO route motion over RTM, but the
**authenticated cloud RTM websocket transport is not implemented**. Building it would let FreeBo drive an
Air 2 / EBO Max, with the honest caveat that those models' motion control rides Enabot's cloud relay (not
fully local, unlike the EBO SE LAN path).
