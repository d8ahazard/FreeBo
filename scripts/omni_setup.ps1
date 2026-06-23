# Set up the ISOLATED venv for the MiniCPM-o 2.6 omni service (vision+audio+speech, real-time streaming).
# Pinned deps (transformers 4.44.2 + audio stack) are kept out of the main FreeBo env on purpose.
# Usage: powershell -ExecutionPolicy Bypass -File scripts/omni_setup.ps1
$ErrorActionPreference = "Continue"
$py = "D:\models\omni-venv\Scripts\python.exe"
if (-not (Test-Path $py)) { python -m venv D:\models\omni-venv }

& $py -m pip install --upgrade pip
# CUDA 12.4 torch stack (matches the main env / 3090)
& $py -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
# MiniCPM-o 2.6 runtime: pinned transformers + the omni audio/video stack
& $py -m pip install "transformers==4.44.2" accelerate huggingface_hub sentencepiece Pillow numpy `
    librosa soundfile "vector_quantize_pytorch==1.18.5" "vocos==0.1.0" decord moviepy fastapi "uvicorn[standard]" websockets
Write-Output "OMNI_SETUP_DONE"
