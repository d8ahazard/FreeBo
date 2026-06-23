# Publishing FreeBo (clean public repo)

FreeBo currently lives inside a larger working tree. To publish a clean, safe public repo, init git **at the
project root** (`e:/dev/autobot`), not at the monorepo root, and verify nothing sensitive or huge is staged.

## What must NEVER be published

- Secrets: `.env`, anything under `vendor/`, `collector/captured/`, `**/captured_secrets.json`.
- App packages / big binaries: `*.apk`, `*.xapk`, `*.aab`, `*.so`, `*.dll`, `*.dylib` (all gitignored).
- TTS voices: `*.onnx`, `*.onnx.json`, `data/voices/` (large; fetched with `scripts/get_voice.py`).
- Runtime data: `data/` (memory, enrolled faces).
- Embedded reference repos: `GrowBot/`, `ebo-se-lan-bridge/` (their own git; reference only — the bits we
  use are already merged into `autobot/robot/` and vendored under `autobot/robot/native/eboproto/`).

These are all covered by `.gitignore`. The 208 MB `ROLA_2.2.0_APKPure.xapk` at the root is caught by `*.xapk`.

## What SHOULD be published

- `autobot/` (the app), `webui/src` + build config, `scripts/`, `docs/`, `deploy/pi-gen/`, `collector/`
  (source only — its build artifacts are ignored), `start.sh`, `start.ps1`, `run.sh`, `Dockerfile`,
  `docker-compose.yml`, `requirements.txt`, `README.md`, `LICENSE`, `.env.example`, `.gitignore`.
- The non-secret deploy template `deploy/.../autobot.env` is intentionally re-included (`!deploy/**/autobot.env`).

## Checklist

```sh
cd e:/dev/autobot
git init
git add -A

# 1. Confirm NOTHING sensitive/huge is staged (should print nothing):
git ls-files | grep -E '\.(env|apk|xapk|aab|so|dll|dylib|onnx)$' | grep -v '\.env\.example$'

# 2. Confirm the embedded repos and vendor/ are not staged:
git ls-files | grep -E '^(vendor/|GrowBot/|ebo-se-lan-bridge/|data/)' || echo "clean"

# 3. Sanity: no file over ~5 MB staged
git ls-files -z | xargs -0 du -h 2>/dev/null | sort -rh | head

# 4. Verify the byte-identity gate passes
python scripts/eboproto_check.py
```

Only commit once steps 1–2 print nothing (besides `.env.example`) and step 4 passes.

> Note: `vendor/` is intentionally fully ignored (it holds device secrets + non-redistributable TUTK libs).
> Setup instructions for what users must place there live in `docs/SETUP.md` and `docs/COLLECTOR.md`, not in
> a tracked `vendor/README.md`.
