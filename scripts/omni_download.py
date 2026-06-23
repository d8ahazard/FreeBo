"""Download the MiniCPM-o 2.6 (int4) omni model for FreeBo's real-time vision+audio+speech brain.

Runs in the MAIN env (which already has huggingface_hub). The model lands on D: (plenty of space). The omni
SERVICE runs in an isolated venv (see scripts/omni_setup.* ) so its pinned deps don't clash with the app.
"""
from __future__ import annotations

import os
import sys

from huggingface_hub import snapshot_download

REPO = os.environ.get("MINICPM_REPO", "openbmb/MiniCPM-o-2_6-int4")
DEST = os.environ.get("MINICPM_DIR", r"D:\models\MiniCPM-o-2_6-int4")


def main() -> int:
    print(f"Downloading {REPO} -> {DEST}", flush=True)
    path = snapshot_download(REPO, local_dir=DEST, resume_download=True,
                             allow_patterns=["*.json", "*.py", "*.model", "*.safetensors", "*.bin",
                                             "*.txt", "tokenizer*", "*.onnx"])
    print(f"DONE: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
