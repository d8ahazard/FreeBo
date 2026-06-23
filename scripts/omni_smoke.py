"""Smoke test: can MiniCPM-o 2.6 (int4) load on this GPU with vision+audio+TTS? Prints OMNI_LOAD_OK or the error."""
import os, traceback
MODEL_DIR = os.environ.get("MINICPM_DIR", r"D:\models\MiniCPM-o-2_6-int4")
try:
    import torch
    from transformers import AutoModel, AutoTokenizer
    print("loading", MODEL_DIR, flush=True)
    m = AutoModel.from_pretrained(MODEL_DIR, trust_remote_code=True, attn_implementation="sdpa",
                                  torch_dtype=torch.bfloat16,
                                  init_vision=True, init_audio=True, init_tts=True)
    m = m.eval().cuda()
    try:
        m.init_tts()
        print("init_tts ok", flush=True)
    except Exception as e:
        print("init_tts warn:", e, flush=True)
    print("VRAM alloc GB:", round(torch.cuda.memory_allocated()/1e9, 2), flush=True)
    print("OMNI_LOAD_OK", flush=True)
except Exception:
    traceback.print_exc()
    print("OMNI_LOAD_FAIL", flush=True)
