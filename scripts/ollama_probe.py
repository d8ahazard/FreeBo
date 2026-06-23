"""Quick capability probe: does the local Ollama model do (1) tool-calling and (2) vision via the
OpenAI-compatible endpoint? The FreeBo brain needs both. Prints PASS/FAIL for each."""
import base64
import io
import json
import time
import urllib.request

BASE = "http://localhost:11434/v1/chat/completions"
MODEL = "qwen2.5vl:7b"


def call(payload):
    req = urllib.request.Request(
        BASE, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
    return out, time.time() - t0


def red_png_b64():
    # 8x8 solid red PNG, hand-built so we need no PIL
    try:
        from PIL import Image
        im = Image.new("RGB", (32, 32), (220, 30, 30))
        buf = io.BytesIO(); im.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        # minimal 1x1 red png
        raw = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGP8z8BQDwAEhQGAhKmM"
               "IQAAAABJRU5ErkJggg==")
        return raw


# --- 1) tool calling ---
tools = [{
    "type": "function",
    "function": {
        "name": "drive",
        "description": "Drive the robot.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["forward", "back", "left", "right"]},
                "duration": {"type": "number"},
            },
            "required": ["direction"],
        },
    },
}]
try:
    out, dt = call({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You control a robot. Use the drive tool to move."},
            {"role": "user", "content": "Drive forward for 1.5 seconds to explore."},
        ],
        "tools": tools,
        "stream": False,
    })
    msg = out["choices"][0]["message"]
    tc = msg.get("tool_calls")
    if tc:
        print(f"TOOLS: PASS ({dt:.1f}s) -> {tc[0]['function']['name']}({tc[0]['function']['arguments']})")
    else:
        print(f"TOOLS: FAIL ({dt:.1f}s) -> no tool_calls; content={msg.get('content')!r}")
except Exception as e:
    print(f"TOOLS: ERROR -> {e}")

# --- 2) vision ---
try:
    b64 = red_png_b64()
    out, dt = call({
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What single color fills this image? One word."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "stream": False,
    })
    ans = out["choices"][0]["message"].get("content", "")
    ok = "red" in ans.lower()
    print(f"VISION: {'PASS' if ok else 'FAIL'} ({dt:.1f}s) -> {ans.strip()[:80]!r}")
except Exception as e:
    print(f"VISION: ERROR -> {e}")

# --- 3) vision + tools together (the real workload) ---
try:
    b64 = red_png_b64()
    out, dt = call({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a robot. Look at the camera frame, then call drive."},
            {"role": "user", "content": [
                {"type": "text", "text": "Here is your camera. Decide a move and call the drive tool."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ],
        "tools": tools,
        "stream": False,
    })
    msg = out["choices"][0]["message"]
    tc = msg.get("tool_calls")
    if tc:
        print(f"VISION+TOOLS: PASS ({dt:.1f}s) -> {tc[0]['function']['name']}({tc[0]['function']['arguments']})")
    else:
        print(f"VISION+TOOLS: FAIL ({dt:.1f}s) -> content={msg.get('content')!r}")
except Exception as e:
    print(f"VISION+TOOLS: ERROR -> {e}")
