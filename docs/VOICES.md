# Voices (FreeBo talkback)

FreeBo speaks through the robot's speaker using **local, fast** text-to-speech. Two engines:

- **Piper** (preferred, `AUTOBOT_TTS_ENGINE=piper`): small neural `.onnx` voices, near real-time on CPU,
  fully offline, and swappable — Jarvis-ish, Hulk-ish, or any voice you drop in.
- **OS** (`AUTOBOT_TTS_ENGINE=os`): Windows SAPI / macOS `say` / Linux espeak-ng. Zero-setup fallback that
  works even with no voices installed.

> The robot's listen codec is **G.711 µ-law @ 8 kHz mono**, so that's the audio quality ceiling regardless
> of voice — the *voice identity* still comes through, but it's telephone-grade fidelity.

## Get voices

```sh
python scripts/get_voice.py --list          # see curated aliases
python scripts/get_voice.py jarvis           # calm British male (Jarvis-ish)
python scripts/get_voice.py hulk             # deep US male (closest open voice to Hulk)
python scripts/get_voice.py --all            # grab the whole curated set
python scripts/get_voice.py en_US-amy-medium # any raw id from rhasspy/piper-voices
```

Voices land in `data/voices/*.onnx` (gitignored). Then pick one in the UI **Config → Voice**, or set
`AUTOBOT_VOICE=jarvis` (the voice id = the `.onnx` filename without extension).

| alias | Piper id | vibe |
|-------|----------|------|
| `jarvis` | `en_GB-alan-medium` | calm British male |
| `hulk` | `en_US-ryan-high` | deep, expressive US male |
| `narrator` | `en_US-lessac-medium` | neutral narrator |
| `amy` | `en_US-amy-medium` | warm US female |
| `british` | `en_GB-alba-medium` | British female |

Browse the full open collection at <https://huggingface.co/rhasspy/piper-voices>. Any `.onnx` + `.onnx.json`
pair placed in `data/voices/` is auto-detected.

## Install Piper

- Binary: download `piper` from <https://github.com/rhasspy/piper/releases> and put it on your PATH, or
- pip: `pip install piper-tts` (provides the `piper` command).

If `piper` isn't found or no `.onnx` voice is present, FreeBo automatically falls back to the OS voice, so
talkback always works.
