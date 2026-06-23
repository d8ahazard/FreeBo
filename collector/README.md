# collector — one-time robot credential capture

The app needs device-specific secrets that only exist while the Enabot/ROLA app talks to your robot. This
folder captures them (using Frida) and writes them into the top-level `.env` + `vendor/`. The AOSP bionic
runtime is bundled here too. Full guide: [../docs/COLLECTOR.md](../docs/COLLECTOR.md).

## Pieces

| Path | What |
|------|------|
| [`hooks/agent.js`](hooks/agent.js) | the shared Frida hook (captures license/uid/authkey/identity/token + the 0x9930 blob; POSTs to the receiver) |
| [`receiver.py`](receiver.py) | LAN listener that assembles captures and writes the top-level `.env` + `vendor/ioctl9930.bin` (stdlib only) |
| [`apk_patch/`](apk_patch/) | **phone path**: patch ROLA with Frida Gadget + extract the TUTK `.so` libs |
| [`pc_frida/`](pc_frida/) | **PC fallback**: Frida via USB phone / emulator |
| [`bionic/`](bionic/) | bundled AOSP 32-bit runtime (Apache-2.0) + fetch script |

## TL;DR

```bash
# always: receiver running on your PC
python collector/receiver.py --port 8400

# phone path (primary):
python collector/apk_patch/patch.py --apk rola.apk --host <PC-LAN-IP> --port 8400
#   -> sideload build/rola-autobot.apk, open it, connect to the robot once

# OR PC fallback:
python collector/pc_frida/run.py --package <rola.pkg> --host 127.0.0.1 --port 8400 --spawn

# then bundle bionic + build the app
cp -r collector/bionic vendor/bionic
docker compose up -d --build
```

Secrets are written only to the top-level `.env` and `vendor/` (both gitignored) and masked in all logs.
