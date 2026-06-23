"""FreeBo vision+reasoning service — Qwen2.5-VL (reasons about the scene, not just yes/no).

This is the VISION/REASONING half of FreeBo's modular brain (hearing = faster-whisper, speech = Piper, both in
the main app). Unlike the old moondream2 (1.8B, no reasoning), Qwen2.5-VL actually understands a scene and
follows instructions, so it returns a real decision WITH its reasoning:

    SEE   : what it actually sees
    THINK : why it's choosing the move (avoid obstacles, head for open space, approach people, ...)
    ACTION: forward | left | right | back | stop | none
    EYES  : an expression

Default model Qwen/Qwen2.5-VL-7B-Instruct (~16 GB fp16 — fits a 24 GB card easily). Set VLM_MODEL to
Qwen/Qwen2.5-VL-3B-Instruct for a lighter/faster option. transformers >= 4.49 is required.

Endpoints (Starlette):
  GET  /health
  POST /vlm/decide   {frames_b64:[...], mode, heard, describe, persona, robot_name, directive}
                     -> {ok, text, action, eyes, person, note}   (VLM-as-whole-brain: see + decide)
  POST /vlm/perceive {frames_b64:[...], robot_name, persona}
                     -> {ok, text}   (hybrid brain: concise SCENE/OBJECTS/PEOPLE/PATHS for the cortex)
  POST /vlm/observe  {frames_b64:[...], heard, robot_name, persona}
                     -> {ok, thoughts}   (free-form think-out-loud, for testing)
"""
from __future__ import annotations

import base64
import io
import os
import re
import threading
import time

os.environ.setdefault("HF_HOME", r"D:\models\hf-cache")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")   # the Xet backend was failing downloads (IncompleteBody)
# Persist the flash-linear-attention Triton kernels across restarts. The first-ever compile is slow
# (~90s); with a warm cache a restart is ~15s, and the startup warmup below pays it before serving.
os.environ.setdefault("TRITON_CACHE_DIR", r"D:\models\triton-cache")
# This model's decode is per-step overhead bound (it's only ~1.3B params); fla's CUDA-graph path cuts the
# per-token kernel-launch overhead noticeably (~150 -> ~120 ms/tok here). Must be set before fla imports.
os.environ.setdefault("FLA_USE_CUDA_GRAPH", "1")
# Torch backend only — a broken TensorFlow/Flax in this env otherwise crashes the transformers import.
os.environ["USE_TF"] = "0"
os.environ["USE_FLAX"] = "0"
os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "1"

MODEL = os.environ.get("VLM_MODEL", "openbmb/MiniCPM-V-4_6")
DS = os.environ.get("VLM_DOWNSAMPLE", "16x")   # MiniCPM-V visual-token downsample; must match in generate()
HOST = os.environ.get("VLM_HOST", "127.0.0.1")
PORT = int(os.environ.get("VLM_PORT", "8360"))
MAX_W = int(os.environ.get("VLM_MAX_W", "640"))   # downscale frames for speed (Qwen handles small images well)
MAX_SLICES = int(os.environ.get("VLM_MAX_SLICES", "2"))  # MiniCPM image slicing: fewer slices => far fewer
#   visual tokens => faster prefill. 2 keeps a global view + light detail, plenty for obstacle/nav decisions.

_model = None
_proc = None
_load_lock = threading.Lock()
_last_turn = {"dir": "right"}
ACTIONS = {"forward", "back", "backward", "left", "right", "stop", "none"}
# Movement guidance (mirror of autobot/brain/motion_model.guidance_text — kept inline because this service
# runs in its own venv). The robot's drivetrain is twitchy with deadbands and no distance sensor, so bias the
# coarse decision toward turning-to-inspect over charging forward; the cerebellum owns the actual speeds.
NAV_GUIDANCE = (
    "You roll on treads and can PIVOT IN PLACE. A low-level controller handles speed and confirms each move, "
    "and it takes only SHORT steps — so you don't need a perfectly empty scene to move. Your DEFAULT is to "
    "EXPLORE: when there's open floor ahead (even a meter or two, partial clutter to the sides is fine), go "
    "'forward' to cover ground. Only 'left'/'right' to turn when something is CLOSE directly ahead or you've "
    "reached a wall/dead end. Don't just spin in place — keep moving into new space.")
EYES = {"neutral", "happy", "sad", "angry", "surprised", "sleepy", "love", "dizzy",
        "blink", "curious", "excited", "scared", "confused", "wink", "cool"}


def _warmup() -> None:
    """MiniCPM-V 4.6's `linear_attention` layers run on flash-linear-attention's Triton kernels. The first
    call JIT-compiles them (slow, then cached on disk via TRITON_CACHE_DIR). Do it here, at load time and
    before we serve, so the brain's first real request hits warm kernels instead of a multi-second compile."""
    try:
        from PIL import Image
        t = time.time()
        _gen(Image.new("RGB", (MAX_W, int(MAX_W * 0.6)), (0, 0, 0)), "Reply only: ACTION: stop", 16)
        print(f"[vlm] warmup done in {time.time() - t:.1f}s", flush=True)
    except Exception as e:  # noqa: BLE001 — warmup is best-effort; never block serving on it
        print(f"[vlm] warmup skipped ({type(e).__name__}: {e})", flush=True)


def load_model():
    global _model, _proc
    if _model is not None:
        return
    with _load_lock:                       # serialize: concurrent requests must not all enter from_pretrained
        if _model is not None:
            return
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor
        print(f"[vlm] loading {MODEL} (first run downloads the weights) ...", flush=True)
        proc = AutoProcessor.from_pretrained(MODEL, trust_remote_code=True)
        # flash_attention_2 speeds up the model's `full_attention` layers; fall back to sdpa if the build
        # doesn't support it so a missing kernel never turns into a hard failure.
        common = dict(dtype=torch.bfloat16, device_map="cuda", trust_remote_code=True)
        try:
            mdl = AutoModelForImageTextToText.from_pretrained(
                MODEL, attn_implementation="flash_attention_2", **common).eval()
            attn = "flash_attention_2"
        except Exception as e:  # noqa: BLE001
            print(f"[vlm] flash_attention_2 unavailable ({type(e).__name__}); using sdpa", flush=True)
            mdl = AutoModelForImageTextToText.from_pretrained(
                MODEL, attn_implementation="sdpa", **common).eval()
            attn = "sdpa"
        _proc, _model = proc, mdl
        print(f"[vlm] ready — VRAM {torch.cuda.memory_allocated()/1e9:.1f} GB, attn={attn}", flush=True)
        _warmup()


def _img(b64: str):
    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    if im.width > MAX_W:
        im = im.resize((MAX_W, int(im.height * MAX_W / im.width)))
    return im


def _gen(img, prompt: str, max_new_tokens: int = 160) -> str:
    import torch
    messages = [{"role": "user", "content": [
        {"type": "image", "image": img}, {"type": "text", "text": prompt}]}]
    # downsample_mode / max_slice_nums are image-processor kwargs: pass them via processor_kwargs (in
    # transformers 5.x loose **kwargs here are deprecated). downsample_mode is repeated to generate() below
    # because the model must merge visual tokens the same way the processor sliced them.
    inputs = _proc.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt",
        processor_kwargs={"downsample_mode": DS, "max_slice_nums": MAX_SLICES}).to(_model.device)
    with torch.no_grad():
        out = _model.generate(**inputs, downsample_mode=DS, max_new_tokens=max_new_tokens, do_sample=False)
    trimmed = [o[len(i):] for i, o in zip(inputs.input_ids, out)]
    return _proc.batch_decode(trimmed, skip_special_tokens=True,
                              clean_up_tokenization_spaces=False)[0].strip()


def _mode_directive(mode: str, directive: str, robot_name: str, persona: str) -> str:
    if mode == "conversational":
        return ("You are talking with someone. Stay put; only turn left/right to keep them centered. "
                "Do NOT drive forward/back.")
    if mode == "command" and directive:
        return (f"Your current goal: {directive}. Pursue it — scan by turning to find the target, then drive "
                f"toward it. ")
    return ("Explore: actively head into open space and through doorways and COVER GROUND — drive forward "
            "whenever there's open floor ahead (take a short step, then re-check). Only turn when something is "
            "close directly ahead or you hit a wall/dead end. Keep moving to new areas; don't linger or spin.")


def _parse(raw: str):
    see = think = ""
    action, eyes = "", ""
    for line in raw.splitlines():
        m = re.match(r"\s*(SEE|THINK|ACTION|EYES)\s*[:\-]\s*(.+)", line, re.I)
        if not m:
            continue
        key, val = m.group(1).upper(), m.group(2).strip()
        if key == "SEE":
            see = val
        elif key == "THINK":
            think = val
        elif key == "ACTION":
            action = (re.split(r"[^a-zA-Z]", val.lower().strip()) or [""])[0]
        elif key == "EYES":
            eyes = (re.split(r"[^a-zA-Z]", val.lower().strip()) or [""])[0]
    return see, think, action, eyes


def _turn():
    d = _last_turn["dir"]
    _last_turn["dir"] = "left" if d == "right" else "right"
    return d


def decide(frames_b64, mode: str = "explore", heard: str = "", language: str = "en",
           describe: bool = False, persona: str = "", robot_name: str = "FreeBo", directive: str = "") -> dict:
    load_model()
    if not frames_b64:
        return {"ok": True, "text": "", "action": "none", "eyes": "curious", "blind": True}
    img = _img(frames_b64[-1])
    persona = (persona or "a curious, friendly companion robot")[:200]
    robot_name = robot_name or "FreeBo"

    # Heard speech: reply in-character (a real conversational answer that reasons about the scene if relevant).
    if heard:
        reply = _gen(img, f"You are {robot_name}, {persona}. You can see the scene above. Someone said to you: "
                          f"\"{heard}\". Reply in ONE short, natural, in-character spoken sentence.", 80)
        return {"ok": True, "text": reply.strip().strip('"'), "action": "none", "eyes": "happy", "spoke": True}

    directive_txt = _mode_directive(mode, directive, robot_name, persona)

    # Decode is the cost (~110 ms/token), so token COUNT is the latency knob. The common roaming cycles
    # (describe off, not a goal-pursuit command) don't use the SEE/THINK text — the brain throws it away and
    # only acts on ACTION/EYES — so generate just those two lines (~10 tokens => ~1.5s instead of ~5s). The
    # full reasoned SEE/THINK output is kept for describe / command / heard cycles where the brain speaks it.
    if not describe and mode != "command":
        prompt = (
            f"You are {robot_name}, a small two-wheeled robot looking through your camera at the image above.\n"
            f"{directive_txt}\n{NAV_GUIDANCE}\n\n"
            "Decide your single next move. Reply in EXACTLY two lines, nothing else:\n"
            "ACTION: <forward|left|right|back|stop>\n"
            "EYES: <neutral|happy|curious|surprised|excited|confused|sad>")
        raw = _gen(img, prompt, 24)
        _see, _think, action, eyes = _parse(raw)
        if action not in ACTIONS:
            action = "forward" if "forward" in raw.lower() else _turn()
        if action == "none":
            action = "stop"
        if eyes not in EYES:
            eyes = "curious"
        if mode == "conversational" and action in ("forward", "back", "backward"):
            action = "none"
        return {"ok": True, "text": "", "action": action, "eyes": eyes, "person": False,
                "note": "", "raw": raw[:200], "fast": True}

    prompt = (
        f"You are {robot_name}, {persona}. You are a small two-wheeled robot looking through your camera at "
        f"the image above.\n{directive_txt}\n{NAV_GUIDANCE}\n\n"
        "Reason about the scene, then decide your single next move. Reply in EXACTLY this format, one item per "
        "line, nothing else:\n"
        "SEE: <one concrete sentence describing what is actually in front of you>\n"
        "THINK: <one short sentence: the smart move and why (mention obstacles/open space)>\n"
        "ACTION: <forward|left|right|back|stop>\n"
        "EYES: <neutral|happy|curious|surprised|excited|confused|sad>")
    raw = _gen(img, prompt, 160)
    see, think, action, eyes = _parse(raw)

    if action not in ACTIONS:
        action = "forward" if "forward" in raw.lower() else _turn()
    if action == "none":
        action = "stop"
    if eyes not in EYES:
        eyes = "curious"
    # conversational guard (also enforced by the safety floor): never translate
    if mode == "conversational" and action in ("forward", "back", "backward"):
        action = "none"

    person = bool(re.search(r"\b(person|people|man|woman|someone|child|kid|human)\b", see, re.I))
    # The robot only SPEAKS aloud when addressed (the `heard` branch above returns spoken `text`). On
    # autonomous cycles we DON'T narrate — the SEE/THINK reasoning still goes to the UI thought feed via
    # `note`, but `text` stays empty so the robot isn't talking every time it moves.
    note = ("SEE: " + see.strip() + (" — THINK: " + think.strip() if think.strip() else "")).strip(" -")
    return {"ok": True, "text": "", "action": action, "eyes": eyes,
            "person": person, "note": note, "raw": raw[:400]}


PERCEIVE_PROMPT = (
    "You are the eyes of {name}, a small curious two-wheeled companion robot. Look at this camera frame and "
    "describe it for the robot's brain. Reply in EXACTLY these four short labeled lines, nothing else:\n"
    "SCENE: <one sentence: the kind of room/space and overall layout>\n"
    "OBJECTS: <notable things you see, comma-separated; or 'nothing notable'>\n"
    "PEOPLE: <people and what they're doing; or 'none'>\n"
    "PATHS: <where the open floor / clear directions are (left/center/right), doorways/openings, and any "
    "obstacle or wall that is close ahead>")


def perceive(frames_b64, robot_name: str = "FreeBo", persona: str = "") -> dict:
    """Hybrid-brain perception: a concise, navigation+companion oriented scene description for the tool-calling
    cortex to read as its eyes. No move decision here — just see + describe."""
    load_model()
    if not frames_b64:
        return {"ok": True, "text": "", "blind": True}
    img = _img(frames_b64[-1])
    name = robot_name or "FreeBo"
    text = _gen(img, PERCEIVE_PROMPT.format(name=name), max_new_tokens=130)
    return {"ok": True, "text": text.strip()}


def observe(frames_b64, heard: str = "", robot_name: str = "FreeBo", persona: str = "") -> dict:
    """Standalone perception+reasoning test: observe the scene (and any heard sound), reason about it, and
    decide what to investigate. Free-form 'think out loud' output for human review."""
    load_model()
    if not frames_b64:
        return {"ok": False, "error": "no frame"}
    img = _img(frames_b64[-1])
    persona = (persona or "a curious, friendly companion robot")[:200]
    sound = (f"You ALSO just heard this through your microphone: \"{heard}\".\n" if heard
             else "You don't hear anything notable right now.\n")
    prompt = (
        f"You are {robot_name}, {persona} — a small wheeled robot seeing through your camera.\n{sound}"
        "Think out loud like a curious, intelligent being:\n"
        "1) OBSERVE: describe specifically what you actually see (objects, layout, people), and connect it "
        "to any sound you heard.\n"
        "2) REASON: what's interesting or worth understanding here?\n"
        "3) DECIDE: what would you like to investigate, approach, or do next — and why?\n"
        "Be concrete about THIS scene; don't be generic.")
    text = _gen(img, prompt, max_new_tokens=320)
    return {"ok": True, "thoughts": text, "heard": heard}


def _build_app():
    import asyncio

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(request: Request):
        return JSONResponse({"ok": _model is not None, "model": MODEL, "ver": "minicpm-v-4.6"})

    async def vlm_observe(request: Request):
        body = await request.json()
        res = await asyncio.to_thread(observe, body.get("frames_b64") or [], body.get("heard", ""),
                                      body.get("robot_name", "FreeBo"), body.get("persona", ""))
        return JSONResponse(res)

    async def vlm_perceive(request: Request):
        body = await request.json()
        res = await asyncio.to_thread(perceive, body.get("frames_b64") or [],
                                      body.get("robot_name", "FreeBo"), body.get("persona", ""))
        return JSONResponse(res)

    async def vlm_decide(request: Request):
        body = await request.json()
        res = await asyncio.to_thread(decide, body.get("frames_b64") or [], body.get("mode", "explore"),
                                      body.get("heard", ""), body.get("language", "en"),
                                      bool(body.get("describe", False)), body.get("persona", ""),
                                      body.get("robot_name", "FreeBo"), body.get("directive", ""))
        return JSONResponse(res)

    return Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/vlm/decide", vlm_decide, methods=["POST"]),
        Route("/vlm/observe", vlm_observe, methods=["POST"]),
        Route("/vlm/perceive", vlm_perceive, methods=["POST"]),
    ])


if __name__ == "__main__":
    import uvicorn
    load_model()   # preload before serving so the download/load happens once, not racing on the first requests
    uvicorn.run(_build_app(), host=HOST, port=PORT)
