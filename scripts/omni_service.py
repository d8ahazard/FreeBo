"""FreeBo omni brain service — MiniCPM-o 2.6 real-time vision + audio + speech (fp16, GPU).

Runs in the ISOLATED omni venv (D:\\models\\omni-venv), separate from the main FreeBo app. Exposes a WebSocket
the FreeBo web layer bridges the robot's LIVE Agora video+audio into; MiniCPM-o watches/listens in ~1s time
slices (its TDM streaming) and, on demand, emits streaming TEXT (for the tool-calling action brain) and SPEECH
audio (played back through the robot speaker).

API (validated against modeling_minicpmo.py): get_sys_prompt(mode='omni') -> streaming_prefill(session_id,
[msg], tokenizer) per slice -> streaming_generate(session_id, tokenizer, generate_audio=True) yields chunks
with .text / .audio_wav / .sampling_rate.

Run (omni venv):  D:\\models\\omni-venv\\Scripts\\python.exe scripts/omni_service.py
Point FreeBo at it with AUTOBOT_OMNI_URL=ws://127.0.0.1:8350/omni .
"""
from __future__ import annotations

import base64
import io
import os
import time
import wave

MODEL_DIR = os.environ.get("MINICPM_DIR", r"D:\models\MiniCPM-o-2_6")
HOST = os.environ.get("OMNI_HOST", "127.0.0.1")
PORT = int(os.environ.get("OMNI_PORT", "8350"))

_model = None
_tokenizer = None


def load_model():
    global _model, _tokenizer
    if _model is not None:
        return
    import torch
    from transformers import AutoModel, AutoTokenizer

    # Low-footprint load: 4-bit (bitsandbytes nf4) by default so MiniCPM-o fits in ~5-6 GB instead of ~16 GB
    # (it was OOM-ing the box at fp16). The vision/audio/TTS submodules are skipped from quantization so they
    # stay correct. Set AUTOBOT_OMNI_4BIT=0 to force full bf16.
    use_4bit = os.environ.get("AUTOBOT_OMNI_4BIT", "1") != "0"
    if use_4bit:
        # MiniCPM-o's custom modeling + accelerate's dispatch issues a redundant model.to(device) at the end
        # of load, which bitsandbytes forbids for 4-bit (the weights are ALREADY on the GPU by then). No-op
        # .to() for quantized models so the load completes.
        import transformers.modeling_utils as _mu
        if not getattr(_mu.PreTrainedModel, "_freebo_safe_to", False):
            _orig_to = _mu.PreTrainedModel.to

            def _safe_to(self, *a, **k):
                try:
                    return _orig_to(self, *a, **k)
                except ValueError as e:
                    if "4-bit" in str(e) or "8-bit" in str(e):
                        return self  # weights already placed by bitsandbytes; the .to is redundant
                    raise
            _mu.PreTrainedModel.to = _safe_to
            _mu.PreTrainedModel._freebo_safe_to = True
    print(f"[omni] loading {MODEL_DIR} (4bit={use_4bit}) ...", flush=True)
    kwargs = dict(trust_remote_code=True, attn_implementation="sdpa",
                  init_vision=True, init_audio=True, init_tts=True)
    if use_4bit:
        from transformers import BitsAndBytesConfig
        kwargs.update(
            torch_dtype=torch.bfloat16,
            quantization_config=BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.bfloat16,
                # keep the perception + speech heads in fp16 — quantizing them hurts quality/stability
                llm_int8_skip_modules=["vpm", "apm", "tts", "resampler", "audio_projection_layer"]),
            # Everything on GPU 0. ("auto" splits with accelerate hooks that don't cover the custom forward's
            # position_ids -> CPU/GPU mismatch; the redundant single-device .to that bnb rejects is no-op'd by
            # our _safe_to patch above.)
            device_map={"": 0},
        )
        _model = AutoModel.from_pretrained(MODEL_DIR, **kwargs).eval()
    else:
        _model = AutoModel.from_pretrained(MODEL_DIR, torch_dtype=torch.bfloat16, **kwargs).eval().cuda()
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, trust_remote_code=True)
    _model.init_tts()
    try:
        _model.tts.float()   # tts vocoder is happier in fp32
    except Exception:  # noqa: BLE001
        pass
    import torch as _t
    if use_4bit and _t.cuda.is_available():
        # The blocked whole-model .to left non-quantized BUFFERS (rotary cos/sin/inv_freq, masks) on CPU,
        # which crashes the forward (position_ids/cos device mismatch). bnb only auto-places the quantized
        # PARAMS — so move the leftover CPU buffers onto the GPU ourselves.
        dev = _t.device("cuda:0")
        moved = 0
        for module in _model.modules():
            for bname, buf in list(getattr(module, "_buffers", {}).items()):
                if buf is not None and buf.device.type == "cpu":
                    module._buffers[bname] = buf.to(dev)
                    moved += 1
        print(f"[omni] moved {moved} CPU buffers -> GPU", flush=True)
    if _t.cuda.is_available():
        print(f"[omni] ready — VRAM {_t.cuda.memory_allocated()/1e9:.1f} GB", flush=True)


def _decode_audio(b64: str):
    """Browser sends 16 kHz mono PCM16 base64 -> float32 ndarray in [-1, 1] (what the processor expects)."""
    import numpy as np
    pcm = np.frombuffer(base64.b64decode(b64), dtype=np.int16).astype("float32") / 32768.0
    return pcm


def _decode_image(b64: str):
    from PIL import Image
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


def init_session(session_id: str, language: str = "en"):
    sys_msg = _model.get_sys_prompt(mode="omni", language=language)
    _model.streaming_prefill(session_id=session_id, msgs=[sys_msg], tokenizer=_tokenizer)


def prefill_slice(session_id: str, image=None, audio=None):
    content = []
    if image is not None:
        content.append(image)
    if audio is not None:
        content.append(audio)
    if not content:
        return
    _model.streaming_prefill(session_id=session_id, msgs=[{"role": "user", "content": content}], tokenizer=_tokenizer)


def generate(session_id: str):
    """Yield ('text', str) and ('speech', wav_bytes, sr) as the model streams its reply + voice."""
    import numpy as np
    res = _model.streaming_generate(session_id=session_id, tokenizer=_tokenizer,
                                    generate_audio=True, sampling=True)
    for r in res:
        text = getattr(r, "text", None)
        if text is None and isinstance(r, dict):
            text = r.get("text")
        if text:
            yield ("text", text)
        wav = getattr(r, "audio_wav", None)
        sr = int(getattr(r, "sampling_rate", 24000) or 24000)
        if wav is not None:
            arr = wav.detach().cpu().numpy() if hasattr(wav, "detach") else np.asarray(wav)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
                wf.writeframes((np.clip(arr, -1, 1) * 32767).astype("int16").tobytes())
            yield ("speech", buf.getvalue(), sr)


def respond(image_b64: str | None, audio_b64: str | None, frames_b64: list | None = None,
            language: str = "en", instruction: str | None = None) -> dict:
    """One turn: feed the robot's frame(s) + the captured utterance audio (+ an optional control instruction),
    return the model's spoken reply (text + a single concatenated speech WAV). Turn-based HTTP avoids the WS
    handshake issues entirely."""
    import numpy as np
    load_model()
    session_id = f"freebo-{int(time.time()*1000)}"
    init_session(session_id, language)
    imgs = []
    if frames_b64:
        imgs = [_decode_image(b) for b in frames_b64 if b]
    elif image_b64:
        imgs = [_decode_image(image_b64)]
    aud = _decode_audio(audio_b64) if audio_b64 else None
    # Feed the (optional) control instruction text, then the frame(s), then the audio, as one user slice.
    content = ([instruction] if instruction else []) + list(imgs) + ([aud] if aud is not None else [])
    if content:
        _model.streaming_prefill(session_id=session_id, msgs=[{"role": "user", "content": content}],
                                 tokenizer=_tokenizer)
    text = ""
    pcm_parts = []
    sr = 24000
    for kind, *rest in generate(session_id):
        if kind == "text":
            text += rest[0]
        else:
            # rest[0] is wav bytes; pull raw pcm back out to concat
            buf = io.BytesIO(rest[0]); sr = rest[1]
            with wave.open(buf, "rb") as wf:
                pcm_parts.append(wf.readframes(wf.getnframes()))
    speech_b64 = ""
    if pcm_parts:
        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
            wf.writeframes(b"".join(pcm_parts))
        speech_b64 = base64.b64encode(out.getvalue()).decode()
    return {"ok": True, "text": text.strip(), "speech_b64": speech_b64, "sr": sr}


def _build_app():
    # Use raw Starlette routes (add_route) rather than FastAPI's typed decorators — the omni venv's
    # FastAPI/pydantic combo mis-binds typed params as query args, so we read the request directly.
    import asyncio

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(request: Request):
        return JSONResponse({"ok": _model is not None, "model": os.path.basename(MODEL_DIR), "ver": "4bit-v5"})

    async def omni_respond(request: Request):
        body = await request.json()
        res = await asyncio.to_thread(respond, body.get("image_b64"), body.get("audio_b64"),
                                      body.get("frames_b64"), body.get("language", "en"),
                                      body.get("instruction"))
        return JSONResponse(res)

    return Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/omni/respond", omni_respond, methods=["POST"]),
    ])


if __name__ == "__main__":
    import uvicorn
    # WS impl: uvicorn's handshake 403s with websockets>=14, so the venv pins websockets==12.0 and we use the
    # default ("auto") websockets impl, which is the battle-tested combo. HOST=0.0.0.0 allows LAN access.
    uvicorn.run(_build_app(), host=HOST, port=PORT)
