# Building the Autobot Pi image (pi-gen, via WSL2)

This builds a flashable Raspberry Pi 4 (64-bit Bookworm Lite) image that boots straight into Autobot:
headless wifi onboarding via **comitup**, then the web UI + setup wizard at `http://autobot.local:8200`.

> There is no native-Windows Pi image builder. pi-gen is a Linux tool; on Windows you run it under **WSL2
> (Debian) + Docker**. (Alternatively build on any Linux box or in CI.)

## What's here

- [`config`](config) — pi-gen config (image name, Bookworm, hostname `autobot`, our stage list).
- [`stage-autobot/`](stage-autobot) — a pi-gen stage that installs the app (venv), mediamtx, comitup, a
  systemd service (`autobot.service`), and mDNS (`avahi`). Secrets are **not** baked in.
- [`build-wsl.sh`](build-wsl.sh) — assembles the app payload, applies WSL2 fixes, and runs the build.

## One-time WSL2 setup

```bash
# in WSL2 Debian:
sudo apt-get update && sudo apt-get install -y git docker.io qemu-user-static binfmt-support
sudo update-binfmts --enable
sudo usermod -aG docker "$USER"     # then restart the shell
```

Clone this repo **into the WSL2 Linux filesystem** (e.g. `~/autobot`), not `/mnt/c/...` — pi-gen's image
extraction fails on NTFS.

## Build

```bash
# 1) build the web UI (Node 18+; on Windows or in WSL)
cd webui && npm install && npm run build && cd ..

# 2) build the image
bash deploy/pi-gen/build-wsl.sh
```

The finished `*-autobot.img.*` lands in `~/pi-gen/deploy/`. Flash it with Raspberry Pi Imager / balena
Etcher / `dd`.

## First boot (fully headless)

1. Pi boots and (with no known wifi) raises an access point **`Autobot-XXXX`**.
2. Join it from a phone/laptop; a captive portal lets you pick your home wifi + password.
3. The Pi joins your wifi. Open **`http://autobot.local:8200`**.
4. The **setup wizard** runs: pick your AI provider (it suggests a fast model for interactions + a heavy
   model for daily memory cleanup), name the robot, set the owner, and do the robot-credential step.

## Notes

- The **robot credentials** (TUTK `EBO_*` + the `.so`/`ioctl9930.bin`) are device-specific and captured at
  runtime by the collector (mDNS) — never baked into the image. The bundled bionic runtime and (later) the
  prebuilt `ebo_bridge` binary are the only native bits in the image.
- mediamtx version is pinned in `stage-autobot/00-autobot/01-run.sh`.
- To passphrase-protect the setup AP, set `ap_password` in `stage-autobot/00-autobot/files/comitup.conf`.
