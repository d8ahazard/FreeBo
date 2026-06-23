# Reproducing FreeBo on another robot

This repo is built to be a **100% reproducible** base you can drop onto another EBO (Air 2 in particular)
for testing. The rule of thumb:

- **Code, tooling, and docs live in git** (this repo is ~1 MB — no binaries).
- **Big binaries live in GitHub Releases** (the prebuilt patched credential-collector APK, an optional
  prebuilt wheelhouse). Fetch them with `scripts/fetch_release_assets.*`.
- **Models are never stored anywhere in the repo** — they're fetched per-machine on first use (HuggingFace
  cache for the VLM, InsightFace models, Piper voices via `scripts/get_voice.py`).
- **No proprietary `.so`** — the Air 2 path (`autobot/robot/air2_native_link.py` + `agora_native.py`) is a
  pure Python + Node reimplementation of the robot's RTC/RTM protocol. The legacy TUTK `.so`/bionic only
  ever belonged to the older EBO SE path and are not needed (or shipped) here.
- **Secrets are per-robot** — every robot has its own credentials; you re-run the collector on each one.
  Nothing in `.env`/`vendor/` is ever committed.

## What you need on each machine

- **Robot box** (can be the same workstation): Python 3.10+, Node.js (for the RTM control sidecar), ffmpeg.
- **Eyes box** (optional, for the hybrid reflex+cortex brain): a GPU machine running the VLM service +
  (optionally) Ollama for the cortex/embeddings. Can be the same machine if it has the VRAM.

## 1. Clone + install

```bash
git clone <your-freebo-remote> freebo && cd freebo

# Python deps for the robot app (incl. the Air 2 native RTC link)
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Web UI + the Node RTM sidecar's deps (agora-rtm-sdk + ws are in webui/package.json)
cd webui && npm install && npm run build && cd ..
```

For the **eyes box** (only if you run the hybrid brain): `pip install -r requirements-ai.txt` (install the
torch/onnxruntime build that matches your CUDA), then `python scripts/vlm_service.py`. See
[AI_BRAIN.md](AI_BRAIN.md).

## 2. Capture this robot's credentials

Each robot needs its own EBO account credentials captured once. Two ways:

- **Build the patched collector app yourself (fully reproducible, redistributes nothing):**
  ```bash
  python collector/receiver.py --port 8400            # serves mDNS + writes .env on capture
  bash scripts/build_collector_apk.sh                  # downloads the OFFICIAL EBO app + tools, injects the
                                                       # Frida gadget + hooks/agent.js, signs it
  # sideload build/signed/*-debugSigned.apk on your phone, open it, connect to your EBO once
  ```
- **Or grab the prebuilt patched APK from the release** (your private convenience, not redistributable):
  ```bash
  scripts/fetch_release_assets.sh                      # downloads freebo-cred-collector-*.apk into release-staging/
  ```

Install it, open it, log into your EBO account, connect to the robot once. The hook finds `autobot.local`
over mDNS and POSTs the secrets to `receiver.py`, which writes the top-level `.env`. Then **uninstall the
patched app** (it's for your device/account only). Details: [COLLECTOR.md](COLLECTOR.md).

## 3. Configure

```bash
cp .env.example .env     # the collector already wrote the EBO_* secret block; add your AI settings
```

Key knobs for an Air 2 (see `.env.example` for the full list):

```
AUTOBOT_ROBOT_LINK=air2_native
AUTOBOT_AI_PROVIDER=hybrid           # VLM eyes + tool-calling cortex (or 'openai' for a single cloud model)
AUTOBOT_VLM_URL=http://<eyes-box>:8360
AUTOBOT_AI_BASE_URL=http://<cortex>:11434/v1
AUTOBOT_AI_MODEL=qwen2.5:7b
```

## 4. Run

```bash
python -m autobot          # or ./start.sh / ./start.ps1, or `docker compose up -d --build`
```

Open `http://<robot-box>:8200`. **Calibrate movement** first (the UI gates autonomous roaming until you do),
enroll yourself as owner for greet-by-name, then set it loose.

## Release-asset convention

`scripts/fetch_release_assets.*` reads `FREEBO_RELEASE_REPO` (e.g. `youruser/freebo`) and `FREEBO_RELEASE_TAG`
(default `latest`) and downloads these asset names if present:

- `freebo-cred-collector-rola.apk` / `freebo-cred-collector-ebohome.apk` — prebuilt patched collector apps.
- `freebo-wheelhouse-<platform>.tar.gz` — optional offline pip wheelhouse for that platform/arch.

Build wheelhouses **on the target architecture** (`pip download -r requirements.txt -d wheels/`) — wheels are
platform-specific, so a Windows wheelhouse won't install on a Pi. For true cross-platform repro use the
`Dockerfile` / `deploy/pi-gen/` image. None of these big files belong in git.
