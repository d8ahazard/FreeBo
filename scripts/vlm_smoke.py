"""Smoke test the light vision model (moondream2): download (to D: cache), load on GPU, time several queries
(warm latency is what matters for the control loop)."""
import os
import time

os.environ.setdefault("HF_HOME", r"D:\models\hf-cache")
import torch
from PIL import Image
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = os.environ.get("VLM_MODEL", "vikhyatk/moondream2")
REV = os.environ.get("VLM_REV", "2024-08-26")

t = time.time()
print(f"loading {MODEL} @ {REV} ...", flush=True)
tok = AutoTokenizer.from_pretrained(MODEL, revision=REV, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(
    MODEL, revision=REV, trust_remote_code=True, torch_dtype=torch.float16).to("cuda").eval()
print("loaded in %.1fs, VRAM %.1f GB" % (time.time() - t, torch.cuda.memory_allocated() / 1e9), flush=True)

img = Image.new("RGB", (640, 384), (40, 60, 90))
qs = [
    "Describe what you see in one short sentence.",
    "Is there a clear, open path directly ahead? Answer only yes or no.",
    "Is a person or pet visible? Answer only yes or no.",
]
enc = model.encode_image(img)
for i, q in enumerate(qs):
    t = time.time()
    a = model.answer_question(enc, q, tok)
    print("Q%d (%.2fs): %s" % (i, time.time() - t, repr(a)[:120]), flush=True)
print("DONE")
