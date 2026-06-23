"""Recognition skill — 'remember who/what it sees' + face-based identity (the pairing dependency).

Face recognition is OPTIONAL and has two interchangeable backends (auto-selected, override with
`AUTOBOT_FACE_BACKEND`=auto|insightface|face_recognition|none):

  - insightface (recommended on a GPU box): ONNX RetinaFace + ArcFace, fast on CUDA, no dlib build pain.
  - face_recognition: the classic dlib-based library (CPU-friendly, heavier to install on Windows).

When a backend is active, every perception tick it detects faces in the camera frame, matches them against
enrolled people, updates the Identity layer (so the obedience policy knows who's present), logs sightings,
and — when it FIRST recognizes a known person — nudges the brain to greet them by name. `enroll_face` is how
you 'pair' the owner ("I am your maker"): it stores the owner's face so the robot recognizes them on sight.

Without any backend the skill still offers `note_sighting` (the vision LLM logs what it sees to memory) so
"remember things it sees" works in a degraded, model-driven way. Nothing here can crash the app.
"""
from __future__ import annotations

import json
import os
import threading
import time
from io import BytesIO
from pathlib import Path

from .base import Skill, SkillContext, ToolDef, fn_schema

FACES_DIR = Path(os.environ.get("AUTOBOT_FACES_DIR", "data/faces"))
RECOG_MIN_INTERVAL = float(os.environ.get("AUTOBOT_RECOG_INTERVAL", "3.0"))  # s between scans (vision is costly)


class _FaceRecognitionBackend:
    """dlib-based `face_recognition`: 128-d encodings, euclidean distance (lower = closer match)."""
    kind = "face_recognition"

    def __init__(self):
        import face_recognition  # type: ignore
        import numpy as np  # type: ignore
        from PIL import Image  # type: ignore
        self._fr, self._np, self._Image = face_recognition, np, Image
        self._tol = float(os.environ.get("AUTOBOT_FACE_TOLERANCE", "0.5"))

    def to_array(self, e):
        return self._np.array(e)

    def embed(self, jpeg: bytes) -> list:
        img = self._Image.open(BytesIO(jpeg)).convert("RGB")
        arr = self._np.array(img)
        locs = self._fr.face_locations(arr)
        return list(self._fr.face_encodings(arr, locs)) if locs else []

    def match(self, known: dict[str, list], enc) -> str | None:
        best_name, best_dist = None, 1e9
        for name, encs in known.items():
            if not encs:
                continue
            dists = self._fr.face_distance(encs, enc)
            d = float(min(dists)) if len(dists) else 1e9
            if d < best_dist:
                best_name, best_dist = name, d
        return best_name if best_dist <= self._tol else None


class _InsightFaceBackend:
    """InsightFace ArcFace: 512-d normed embeddings, cosine similarity (higher = closer match). GPU-friendly."""
    kind = "insightface"

    def __init__(self):
        import numpy as np  # type: ignore
        from PIL import Image  # type: ignore
        from insightface.app import FaceAnalysis  # type: ignore
        self._np, self._Image = np, Image
        self._sim = float(os.environ.get("AUTOBOT_FACE_SIM", "0.35"))   # cosine threshold
        # CPU by default (works everywhere; face scans are infrequent). Set AUTOBOT_FACE_GPU=1 to use CUDA —
        # onnxruntime advertises CUDA even when its DLLs can't load, so opting in is explicit, not detected.
        use_gpu = os.environ.get("AUTOBOT_FACE_GPU", "0") == "1"
        providers = (["CUDAExecutionProvider", "CPUExecutionProvider"] if use_gpu else ["CPUExecutionProvider"])
        self._app = FaceAnalysis(name=os.environ.get("AUTOBOT_INSIGHTFACE_MODEL", "buffalo_l"),
                                 providers=providers)
        self._app.prepare(ctx_id=0 if use_gpu else -1, det_size=(640, 640))

    def to_array(self, e):
        return self._np.asarray(e, dtype=self._np.float32)

    def _bgr(self, jpeg: bytes):
        img = self._Image.open(BytesIO(jpeg)).convert("RGB")
        return self._np.array(img)[:, :, ::-1]   # RGB -> BGR for insightface

    def embed(self, jpeg: bytes) -> list:
        faces = self._app.get(self._bgr(jpeg))
        out = []
        for f in faces:
            e = getattr(f, "normed_embedding", None)
            if e is None:
                e = f.embedding
            out.append(self._np.asarray(e, dtype=self._np.float32))
        return out

    def _norm(self, v):
        n = self._np.linalg.norm(v)
        return v / n if n else v

    def match(self, known: dict[str, list], enc) -> str | None:
        en = self._norm(enc)
        best_name, best_sim = None, -1.0
        for name, encs in known.items():
            for k in encs:
                sim = float(self._np.dot(en, self._norm(k)))
                if sim > best_sim:
                    best_name, best_sim = name, sim
        return best_name if best_sim >= self._sim else None


def _load_backend():
    """Pick a face backend per AUTOBOT_FACE_BACKEND (default auto: insightface first, then face_recognition)."""
    pref = os.environ.get("AUTOBOT_FACE_BACKEND", "auto").strip().lower()
    if pref == "none":
        return None
    order = {"auto": ("insightface", "face_recognition"),
             "insightface": ("insightface",), "face_recognition": ("face_recognition",)}.get(pref, ("insightface", "face_recognition"))
    for kind in order:
        try:
            return _InsightFaceBackend() if kind == "insightface" else _FaceRecognitionBackend()
        except Exception:  # noqa: BLE001 - optional dep / no GPU; try the next one
            continue
    return None


class RecognitionSkill(Skill):
    name = "recognition"

    def __init__(self):
        self._backend = _load_backend()
        self.have_faces = self._backend is not None
        self._lock = threading.Lock()
        self._known: dict[str, list] = {}   # name -> list of encodings (numpy arrays)
        self._last_scan = 0.0
        self._announced = False
        self._present_known: set[str] = set()   # who we recognized last scan (for greet-on-arrival)
        if self.have_faces:
            self._load_known()

    def available(self, ctx: SkillContext) -> tuple[bool, str]:
        # Always active: degraded (note_sighting only) if the face lib is missing.
        if self.have_faces and not self._announced:
            ctx.identity.set_recognizer_active(True)
            self._announced = True
        return True, ""

    def system_prompt_fragment(self, ctx: SkillContext) -> str:
        if self.have_faces:
            return ("VISION MEMORY: you recognize enrolled faces automatically (the names in 'People you can "
                    "see right now' are real recognitions). When you FIRST notice someone you know, greet them "
                    "warmly BY NAME like a pet greeting its family. Use `enroll_face` to learn a new person "
                    "(the owner pairs themselves this way). Use `note_sighting` to remember a notable object.")
        return ("VISION MEMORY: face recognition isn't installed, so rely on what you can describe. Use "
                "`note_sighting` to remember a notable person/object you see for later.")

    def tools(self, ctx: SkillContext) -> list[ToolDef]:
        tools = [
            ToolDef(fn_schema("note_sighting", "Remember that you saw a notable person or object (logged to memory).", {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Short name, e.g. 'red backpack' or 'a tabby cat'."},
                    "kind": {"type": "string", "enum": ["object", "person", "pet", "place"], "default": "object"},
                    "detail": {"type": "string", "description": "Optional extra detail."},
                },
                "required": ["label"],
            }), self._make_note(ctx), authority="anyone"),
            ToolDef(fn_schema("who_do_you_see", "Report who/what you currently recognize in view.", {"type": "object", "properties": {}}),
                    self._make_who(ctx), authority="anyone"),
        ]
        if self.have_faces:
            tools.append(ToolDef(fn_schema("enroll_face", "Learn the face currently centered in the camera and tie it to a name (used to pair the owner).", {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Who this face belongs to."}},
                "required": ["name"],
            }), self._make_enroll(ctx), authority="owner"))
        return tools

    # --- face store ---
    def _load_known(self):
        try:
            FACES_DIR.mkdir(parents=True, exist_ok=True)
            for p in FACES_DIR.glob("*.json"):
                data = json.loads(p.read_text(encoding="utf-8") or "[]")
                self._known[p.stem] = [self._backend.to_array(e) for e in data]
        except Exception as e:  # noqa: BLE001
            print(f"[recognition] load faces failed: {e}", flush=True)

    def _save_known(self, name: str):
        try:
            FACES_DIR.mkdir(parents=True, exist_ok=True)
            (FACES_DIR / f"{name}.json").write_text(
                json.dumps([e.tolist() for e in self._known.get(name, [])]), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"[recognition] save face failed: {e}", flush=True)

    # --- per-tick hook ---
    async def on_observe(self, ctx: SkillContext, observation) -> None:
        if observation is not None and getattr(observation, "jpeg", None):
            ctx.flags["last_jpeg"] = observation.jpeg
        if not self.have_faces or not getattr(observation, "jpeg", None):
            return
        now = time.time()
        if now - self._last_scan < RECOG_MIN_INTERVAL:
            return
        self._last_scan = now
        try:
            import asyncio
            names = await asyncio.to_thread(self._scan, observation.jpeg)
        except Exception as e:  # noqa: BLE001
            print(f"[recognition] scan failed: {e}", flush=True)
            return
        known_now = [n for n in names if n != "unknown"]
        if not known_now:
            self._present_known = set()
            return
        ctx.identity.set_present(known_now)
        for n in names:
            ctx.memory.log_sighting(n, kind="person")
        # Greet-on-arrival: someone we know just appeared who wasn't here last scan -> nudge the brain.
        newly = [n for n in known_now if n not in self._present_known]
        self._present_known = set(known_now)
        if newly:
            who = ", ".join(newly)
            await ctx.emit({"type": "thought", "text": f"(recognized {who})", "ts": time.time()})
            if ctx.wake:
                ctx.wake()

    def _scan(self, jpeg: bytes) -> list[str]:
        with self._lock:
            encs = self._backend.embed(jpeg)
            return [self._backend.match(self._known, e) or "unknown" for e in encs]

    # --- handlers ---
    def _make_note(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            label = str(a.get("label", "")).strip()
            if not label:
                return {"ok": False, "error": "label required"}
            ctx.memory.log_sighting(label, kind=str(a.get("kind", "object")), detail=str(a.get("detail", "")))
            return {"ok": True, "noted": label}
        return h

    def _make_who(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            if self.have_faces:
                people = ctx.identity.present_people()
                return {"ok": True, "recognize": people or "no enrolled faces in view",
                        "note": "Anyone unrecognized: describe them from the camera."}
            return {"ok": True, "recognize": "face recognition not installed",
                    "note": "Describe who/what you see from the camera frame."}
        return h

    def _make_enroll(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            name = str(a.get("name", "")).strip()
            if not name:
                return {"ok": False, "error": "name required"}
            jpeg = ctx.flags.get("last_jpeg")
            if not jpeg:
                return {"ok": False, "error": "no camera frame available to enroll"}
            try:
                import asyncio
                ok = await asyncio.to_thread(self._enroll, name, jpeg)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if not ok:
                return {"ok": False, "error": "no face found in the current frame"}
            ctx.memory.remember(f"{name}'s face is enrolled.", kind="person", source="owner")
            return {"ok": True, "enrolled": name}
        return h

    def _enroll(self, name: str, jpeg: bytes) -> bool:
        with self._lock:
            encs = self._backend.embed(jpeg)
            if not encs:
                return False
            self._known.setdefault(name, []).append(encs[0])
            self._save_known(name)
            return True
