# Launch the FreeBo vision service (scripts/vlm_service.py) on its dedicated venv.
#
# Why a dedicated venv: MiniCPM-V 4.6's text backbone is a Qwen3.5 hybrid whose `linear_attention` layers
# only run fast with flash-linear-attention (fla) Triton kernels. fla needs a torch/triton pair where
# torch.compile actually works; the global Python had a broken torch 2.5.1 + triton 3.2.0 combo. This venv
# pins a coherent stack: torch 2.7.1+cu126 + triton-windows 3.3 + transformers 5.12.1 + fla.
#
# Triton JIT-compiles the fla kernels on first use (slow, ~90s the very first time) and caches them under
# TRITON_CACHE_DIR, so later starts are ~15s; the service also warms the kernels before it serves.

$ErrorActionPreference = "SilentlyContinue"

$Py        = if ($env:VLM_PYTHON) { $env:VLM_PYTHON } else { "D:\vlm-venv\Scripts\python.exe" }
$Port      = if ($env:VLM_PORT)   { $env:VLM_PORT }   else { "8360" }
$env:HF_HOME          = if ($env:HF_HOME)          { $env:HF_HOME }          else { "D:\models\hf-cache" }
$env:TRITON_CACHE_DIR = if ($env:TRITON_CACHE_DIR) { $env:TRITON_CACHE_DIR } else { "D:\models\triton-cache" }

$repo = Split-Path -Parent $PSScriptRoot

# Free the port + kill any prior vlm_service before (re)starting.
$c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($c) { foreach ($x in $c) { Stop-Process -Id $x.OwningProcess -Force -ErrorAction SilentlyContinue } }
Get-Process python -ErrorAction SilentlyContinue | Where-Object {
    (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine -like '*vlm_service*'
} | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "[run_vlm] starting $Py scripts/vlm_service.py on port $Port (HF_HOME=$($env:HF_HOME))"
& $Py "$repo\scripts\vlm_service.py"
