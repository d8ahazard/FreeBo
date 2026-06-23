"""End-to-end omni streaming test: load -> init omni session -> prefill (frame + prompt) -> streaming_generate.
Prints the streamed TEXT and how many bytes of SPEECH audio came back. Proves the realtime pipeline works."""
import os, io, traceback
os.environ.setdefault("MINICPM_DIR", r"D:\models\MiniCPM-o-2_6")
try:
    import numpy as np
    from PIL import Image, ImageDraw
    import importlib.util
    spec = importlib.util.spec_from_file_location("omni_service", os.path.join(os.path.dirname(__file__), "omni_service.py"))
    S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)

    S.load_model()
    sid = "test-omni"
    S.init_session(sid)

    # a simple synthetic scene + a spoken-style instruction (str content is allowed in a slice)
    im = Image.new("RGB", (448, 448), (40, 44, 60)); d = ImageDraw.Draw(im)
    d.rectangle([260, 80, 400, 360], fill=(120, 90, 70)); d.ellipse([60, 120, 180, 240], fill=(200, 180, 60))
    S.prefill_slice(sid, image=im, audio=None)
    S._model.streaming_prefill(session_id=sid, msgs=[{"role": "user", "content": ["Briefly, what do you see?"]}], tokenizer=S._tokenizer)

    text = ""; audio_bytes = 0; sr = 0
    for kind, *rest in S.generate(sid):
        if kind == "text":
            text += rest[0]
        else:
            audio_bytes += len(rest[0]); sr = rest[1]
    print("TEXT:", text.strip()[:200], flush=True)
    print(f"SPEECH: {audio_bytes} wav bytes @ {sr} Hz", flush=True)
    print("OMNI_STREAM_OK" if (text or audio_bytes) else "OMNI_STREAM_EMPTY", flush=True)
except Exception:
    traceback.print_exc()
    print("OMNI_STREAM_FAIL", flush=True)
