#!/usr/bin/env python3
"""Download Piper TTS voices into data/voices/ for FreeBo's talkback.

Piper voices are small neural `.onnx` models (+ a `.onnx.json` config) from the open
rhasspy/piper-voices collection. Drop any here and pick it in the UI (or set AUTOBOT_VOICE).

Usage:
  python scripts/get_voice.py --list
  python scripts/get_voice.py jarvis            # download a curated alias
  python scripts/get_voice.py en_GB-alan-medium # download by raw Piper voice id
  python scripts/get_voice.py --all             # grab the whole curated set

Voices are large-ish (~20-60 MB) and gitignored (see .gitignore: data/voices/, *.onnx).
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VOICES_DIR = Path(os.environ.get("AUTOBOT_VOICES_DIR", str(REPO / "data" / "voices")))
BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Curated aliases -> (raw Piper voice id, HF relative dir, blurb). The *-high and libritts/hfc voices are the
# most natural (least "robotic"); all run faster than real time on CPU and instantly on a GPU box.
CATALOG: dict[str, tuple[str, str, str]] = {
    # --- most natural (recommended) ---
    "natural":  ("en_US-libritts_r-medium", "en/en_US/libritts_r/medium", "★ very natural US (multi-speaker) — best default"),
    "female":   ("en_US-hfc_female-medium",  "en/en_US/hfc_female/medium",  "★ natural warm US female"),
    "male":     ("en_US-hfc_male-medium",    "en/en_US/hfc_male/medium",    "★ natural US male"),
    "ryan":     ("en_US-ryan-high",          "en/en_US/ryan/high",          "expressive US male (high quality)"),
    "amy":      ("en_US-amy-medium",         "en/en_US/amy/medium",         "warm US female"),
    "lessac":   ("en_US-lessac-high",        "en/en_US/lessac/high",        "clear US narrator (high quality)"),
    # --- fun / character ---
    "jarvis":   ("en_GB-alan-medium",        "en/en_GB/alan/medium",        "calm British male — Jarvis-ish"),
    "hulk":     ("en_US-ryan-high",          "en/en_US/ryan/high",          "deep expressive US male — Hulk-ish"),
    "british":  ("en_GB-alba-medium",        "en/en_GB/alba/medium",        "British female"),
    "narrator": ("en_US-lessac-medium",      "en/en_US/lessac/medium",      "neutral US narrator"),
}


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  -> {url}")
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:  # noqa: S310 - fixed trusted host
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)


def fetch(voice_id: str, rel_dir: str) -> None:
    onnx = VOICES_DIR / f"{voice_id}.onnx"
    cfg = VOICES_DIR / f"{voice_id}.onnx.json"
    print(f"Fetching {voice_id} ...")
    _download(f"{BASE}/{rel_dir}/{voice_id}.onnx", onnx)
    _download(f"{BASE}/{rel_dir}/{voice_id}.onnx.json", cfg)
    print(f"  saved {onnx} ({onnx.stat().st_size // 1024} KB)")


def _rel_dir_from_id(voice_id: str) -> str | None:
    # e.g. en_US-amy-medium -> en/en_US/amy/medium
    try:
        loclang, name, quality = voice_id.split("-")
        lang = loclang.split("_")[0]
        return f"{lang}/{loclang}/{name}/{quality}"
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Download Piper voices for FreeBo")
    ap.add_argument("voice", nargs="?", help="alias (see --list) or a raw Piper voice id")
    ap.add_argument("--list", action="store_true", help="list curated aliases")
    ap.add_argument("--all", action="store_true", help="download the whole curated set")
    args = ap.parse_args()

    if args.list:
        print("Curated voices (alias -> id : description):")
        for alias, (vid, _rel, blurb) in CATALOG.items():
            print(f"  {alias:10s} -> {vid:24s} {blurb}")
        print("\nOr pass any raw id from https://huggingface.co/rhasspy/piper-voices")
        return 0

    targets: list[tuple[str, str]] = []
    if args.all:
        targets = [(vid, rel) for (vid, rel, _b) in CATALOG.values()]
    elif args.voice in CATALOG:
        vid, rel, _b = CATALOG[args.voice]
        targets = [(vid, rel)]
    elif args.voice:
        rel = _rel_dir_from_id(args.voice)
        if not rel:
            print(f"'{args.voice}' is not a known alias and not a parseable voice id "
                  f"(expected like en_US-amy-medium). Try --list.")
            return 2
        targets = [(args.voice, rel)]
    else:
        ap.print_help()
        return 1

    try:
        for vid, rel in targets:
            fetch(vid, rel)
    except Exception as e:  # noqa: BLE001
        print(f"download failed: {e}", file=sys.stderr)
        return 1
    print(f"\nDone. Voices in {VOICES_DIR}. Pick one in the UI (Config) or set AUTOBOT_VOICE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
