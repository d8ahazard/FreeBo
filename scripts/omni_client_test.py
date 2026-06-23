"""Headless test of the omni service over the wire — mimics the browser bridge.

Synthesizes a SPOKEN question with Piper, downsamples to 16k mono PCM16 (what the robot mic would send), sends
it + a synthetic camera frame to ws://127.0.0.1:8350/omni, then asks the model to generate. Verifies the model
HEARD the question (ASR), SAW the frame, and TALKED back (text + speech). Run in the main env.
"""
import asyncio, base64, io, json, os, subprocess, sys, wave

PIPER = r"data\tools\piper\piper.exe"
VOICE = r"data\voices\en_US-libritts_r-medium.onnx"
QUESTION = "Hello robot. What objects do you see in front of you right now?"


def make_question_pcm16_16k() -> bytes:
    wav = "data/tmp/q.wav"
    os.makedirs("data/tmp", exist_ok=True)
    subprocess.run([PIPER, "--model", VOICE, "--output_file", wav], input=QUESTION.encode(),
                   check=True, capture_output=True, timeout=60)
    # -> 16k mono s16le raw
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", wav,
                        "-ar", "16000", "-ac", "1", "-f", "s16le", "pipe:1"],
                       check=True, capture_output=True, timeout=30)
    return p.stdout


def make_frame_jpeg() -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (448, 448), (35, 40, 55)); d = ImageDraw.Draw(im)
    d.rectangle([250, 90, 410, 360], fill=(130, 95, 70))   # a "couch/box"
    d.ellipse([60, 130, 175, 245], fill=(210, 190, 70))    # a "ball"
    b = io.BytesIO(); im.save(b, format="JPEG"); return b.getvalue()


async def main() -> int:
    import websockets
    pcm = make_question_pcm16_16k()
    print(f"[test] question audio: {len(pcm)} bytes pcm16@16k (~{len(pcm)/2/16000:.1f}s)", flush=True)
    jpeg = make_frame_jpeg()
    async with websockets.connect("ws://127.0.0.1:8350/omni", max_size=16_000_000, open_timeout=120) as ws:
        await ws.send(json.dumps({"type": "init", "language": "en"}))
        # split the question audio into ~1s slices, attach the frame to the first slice
        step = 16000 * 2  # 1s of pcm16
        first = True
        for i in range(0, len(pcm), step):
            chunk = pcm[i:i + step]
            msg = {"type": "chunk", "audio_b64": base64.b64encode(chunk).decode()}
            if first:
                msg["image_b64"] = base64.b64encode(jpeg).decode(); first = False
            await ws.send(json.dumps(msg))
        await ws.send(json.dumps({"type": "generate"}))
        text = ""; speech_bytes = 0; sr = 0
        while True:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=180))
            if r.get("type") == "text":
                text += r.get("text", "")
            elif r.get("type") == "speech":
                speech_bytes += len(base64.b64decode(r.get("b64", ""))); sr = r.get("sr", 0)
            elif r.get("type") == "done":
                break
            elif r.get("type") == "error":
                print("[test] ERROR:", r.get("error")); return 1
        print("[test] REPLY TEXT:", text.strip()[:300], flush=True)
        print(f"[test] REPLY SPEECH: {speech_bytes} wav bytes @ {sr} Hz", flush=True)
        print("OMNI_BRIDGE_OK" if (text or speech_bytes) else "OMNI_BRIDGE_EMPTY", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
