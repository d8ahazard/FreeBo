#!/usr/bin/env bash
# Set up the all-in-WSL server-side omni pipeline env: CUDA torch + MiniCPM-o omni stack + Agora server SDK.
# The 3090 is reachable via WSL CUDA passthrough; the model lives on /mnt/d (no re-download).
set -e
echo "WSL_OMNI_SETUP_START"
sudo apt-get update -y >/dev/null 2>&1 || true
sudo apt-get install -y python3-venv python3-pip ffmpeg libnuma1 >/dev/null 2>&1 || true

python3 -m venv "$HOME/freebo-omni"
source "$HOME/freebo-omni/bin/activate"
pip install --upgrade pip
# CUDA 12.4 torch (Linux) for the 3090 via WSL passthrough
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
# MiniCPM-o 2.6 omni runtime (fp16 path: no gptq)
pip install "transformers==4.44.2" accelerate sentencepiece pillow numpy \
    librosa soundfile "vector_quantize_pytorch==1.18.5" "vocos==0.1.0" decord moviepy
# Agora server SDK (Linux) — joins the robot's Agora channel server-side for video+audio
pip install agora-python-server-sdk
# FastAPI for the in-process API the FreeBo UI reads
pip install fastapi "uvicorn[standard]" httpx
echo "WSL_OMNI_SETUP_DONE"
