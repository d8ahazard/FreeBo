# Autobot arm64 image build (proven path)

This is the build path that has been run end-to-end to produce a flashable image. It layers Autobot onto a
known-good **arm64 pi-gen base** rather than fighting upstream pi-gen's armhf/i386 defaults under Docker
Desktop.

## Why a base fork

Upstream `RPi-Distro/pi-gen` defaults to 32-bit `armhf` and uses an `i386/debian:trixie` builder image; under
Docker Desktop/WSL2 the binfmt + i386 handling fails (`qemu-arm not found`, `armhf: not supported`). A
working arm64 pi-gen fork instead uses:

- an **amd64 `debian:bookworm`** builder image,
- host registration of **`qemu-aarch64-static` with the `:F` (fix-binary) binfmt flag** so ARM binaries run
  in the build container,
- **comitup** already wired for headless wifi.

`build.sh` rsyncs that base, drops its app-specific substages, sets the Autobot `config`, fixes the
first-boot user, and adds the `05-autobot` install substage (app + venv + mediamtx + systemd service).

## Files

- [`build.sh`](build.sh) — orchestrator. `GLIMMR_PIGEN=<arm64 pi-gen dir> bash build.sh`.
- [`config`](config) — image name / hostname (`autobot`) / first-boot user.
- [`05-autobot/01-packages`](05-autobot/01-packages) — apt deps (python3, ffmpeg, espeak-ng, avahi, ...).
- [`05-autobot/02-run.sh`](05-autobot/02-run.sh) — installs the app payload, venv (PyPI, not piwheels),
  mediamtx arm64, comitup.conf, `autobot.service`. Reuses the static files from `../stage-autobot/00-autobot/files`.

## Prereqs (WSL2)

```bash
sudo apt-get install -y qemu-user-static binfmt-support rsync git   # NOT qemu-user-binfmt (conflicts)
# Docker Desktop with the distro's WSL integration enabled.
```

## Build

```bash
cd webui && npm install && npm run build && cd ..        # build the UI first
GLIMMR_PIGEN=/path/to/arm64-pi-gen bash deploy/pi-gen/glimmr/build.sh
```

Output: `~/autobot-img/deploy/image_*-autobot-lite.zip`. Flash the `.zip` directly with Raspberry Pi Imager
or Etcher. First boot: comitup AP `autobot-<nnn>` -> pick wifi -> `http://autobot.local:8200` -> setup wizard.

Expect ~30-60 min: `apt`/`pip` run under QEMU emulation.
