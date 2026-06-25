"""Places skill — approximate "manual mapping" for monocular EBO robots.

These robots have one camera and no LIDAR/odometry, so there's no metric floor-plan map. What IS useful and
honest: a set of named **places**, each saved with a reference camera snapshot. The owner drives FreeBo to a
spot and saves it ("dock", "kitchen", "front door"); later the AI can be asked to go there and navigate
visually, and `where_am_i` does best-effort **visual place-recognition** (perceptual-hash match of the live
frame against saved places). This is the realistic stand-in for V-SLAM on this hardware (see docs/NAVIGATION.md).

Everything degrades gracefully: place storage works with no extra deps; the visual match uses Pillow if it's
installed (already an optional dep for face recognition) and is simply skipped otherwise.
"""
from __future__ import annotations

import json
import math
import os
import time
from io import BytesIO
from pathlib import Path

from .base import Skill, SkillContext, ToolDef, fn_schema

# Best-effort visual nav tuning. Monocular pose drifts, so moves stay small and we re-check the camera.
_ARRIVE_BITS = 6        # ahash distance <= this => "I'm looking at the saved view" => arrived
_TURN_TOLERANCE = 25.0  # deg of heading error we tolerate before forward (vs. turning toward the target)

PLACES_DIR = Path(os.environ.get("AUTOBOT_PLACES_DIR", "data/places"))
THINGS_PATH = PLACES_DIR / "things.jsonl"   # rolling log of where the robot last saw named things
GRAPH_PATH = PLACES_DIR / "graph.jsonl"     # topological edges: which places connect to which (auto-built)


def _try_pil():
    try:
        from PIL import Image  # type: ignore
        return Image
    except Exception:  # noqa: BLE001
        return None


def _ahash(Image, jpeg: bytes) -> str | None:
    """8x8 average perceptual hash -> 16-hex string. Cheap, rotation/scale-sensitive but fine for 'is this
    roughly the same view'."""
    try:
        img = Image.open(BytesIO(jpeg)).convert("L").resize((8, 8))
        px = list(img.getdata())
        avg = sum(px) / len(px)
        bits = 0
        for i, p in enumerate(px):
            if p >= avg:
                bits |= 1 << i
        return f"{bits:016x}"
    except Exception:  # noqa: BLE001
        return None


def _hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:  # noqa: BLE001
        return 64


class PlacesSkill(Skill):
    name = "places"

    def __init__(self):
        self._Image = _try_pil()
        self._last_place: str | None = None   # for auto-building the topological place graph (graph.jsonl)
        PLACES_DIR.mkdir(parents=True, exist_ok=True)

    def system_prompt_fragment(self, ctx: SkillContext) -> str:
        names = self._names()
        known = f" Known places: {', '.join(names)}." if names else ""
        return ("PLACES & THINGS: you can `save_place` the current spot, `list_places`, `go_to_place` (takes "
                "one small step toward a saved place each call — keep calling it and re-check until it reports "
                "arrived), `where_am_i` to recognize a saved place. To remember WHERE objects are, call "
                "`remember_thing` when you notice something notable (e.g. 'my charger', 'a backpack', 'the "
                "couch'); later call `where_is` to recall the last place you saw it. Build up a mental map of "
                f"the space as you explore.{known}")

    def tools(self, ctx: SkillContext) -> list[ToolDef]:
        return [
            ToolDef(fn_schema("save_place", "Save the robot's current spot as a named place (stores a reference camera snapshot).", {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Short place name, e.g. 'kitchen' or 'dock'."}},
                "required": ["name"],
            }), self._make_save(ctx), authority="owner"),
            ToolDef(fn_schema("list_places", "List the named places you've saved.", {"type": "object", "properties": {}}),
                    self._make_list(ctx), authority="anyone"),
            ToolDef(fn_schema("go_to_place", "Head toward a saved place. There's no map: navigate visually with small moves, re-checking the camera.", {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }), self._make_goto(ctx), authority="owner"),
            ToolDef(fn_schema("where_am_i", "Recognize whether the current view matches a saved place (best-effort visual match).", {"type": "object", "properties": {}}),
                    self._make_where(ctx), authority="anyone"),
            ToolDef(fn_schema("remember_thing", "Record WHERE you currently see a notable object/thing, so you can find it later. Ties it to your current place + a visual fingerprint.", {
                "type": "object",
                "properties": {
                    "thing": {"type": "string", "description": "What it is, e.g. 'my charger', 'a red backpack', 'the couch'."},
                    "note": {"type": "string", "description": "Optional extra detail (e.g. 'on the floor by the door')."},
                },
                "required": ["thing"],
            }), self._make_remember_thing(ctx), authority="anyone"),
            ToolDef(fn_schema("where_is", "Recall the last place(s) you saw a thing. Use to find objects you've noted before.", {
                "type": "object",
                "properties": {"thing": {"type": "string"}},
                "required": ["thing"],
            }), self._make_where_is(ctx), authority="anyone"),
        ]

    # --- storage ---
    def _names(self) -> list[str]:
        try:
            return sorted(p.stem for p in PLACES_DIR.glob("*.json"))
        except Exception:  # noqa: BLE001
            return []

    def _load(self, name: str) -> dict | None:
        p = PLACES_DIR / f"{name}.json"
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    async def _grab_jpeg(self, ctx: SkillContext) -> bytes | None:
        jpeg = ctx.flags.get("last_jpeg")
        if jpeg:
            return jpeg
        try:
            data, _ = await ctx.link.snapshot()
            return data
        except Exception:  # noqa: BLE001
            return None

    def _make_save(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            name = str(a.get("name", "")).strip().lower().replace("/", "_")
            if not name:
                return {"ok": False, "error": "name required"}
            jpeg = await self._grab_jpeg(ctx)
            meta = {"name": name, "ts": time.time(),
                    "ahash": (_ahash(self._Image, jpeg) if (self._Image and jpeg) else None),
                    "pose": self._pose(ctx)}   # rough VSLAM pose so go_to_place can steer by bearing
            try:
                if jpeg:
                    (PLACES_DIR / f"{name}.jpg").write_bytes(jpeg)
                (PLACES_DIR / f"{name}.json").write_text(json.dumps(meta), encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"save failed: {e}"}
            ctx.memory.remember(f"Saved the place '{name}'.", kind="place", source="owner")
            self._visited(name)   # we're standing here now (records a graph edge from the last place)
            return {"ok": True, "saved": name, "has_snapshot": bool(jpeg), "has_pose": bool(meta["pose"])}
        return h

    # --- pose + topological graph helpers ---
    def _pose(self, ctx: SkillContext) -> dict | None:
        """Current rough VSLAM pose {x,y,yaw_deg}, or None if SLAM isn't available."""
        try:
            p = ctx.pose_provider() if getattr(ctx, "pose_provider", None) else None
            if isinstance(p, dict) and p.get("enabled") and p.get("pose"):
                return p.get("pose")
        except Exception:  # noqa: BLE001
            pass
        return None

    def _visited(self, name: str) -> None:
        """Mark that the robot is now at `name`; append a topological edge from the previous place."""
        prev, self._last_place = self._last_place, name
        if prev and prev != name:
            try:
                with GRAPH_PATH.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"from": prev, "to": name, "ts": time.time()}) + "\n")
            except Exception:  # noqa: BLE001
                pass

    def _make_list(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            return {"ok": True, "places": self._names()}
        return h

    def _make_goto(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            name = str(a.get("name", "")).strip().lower()
            meta = self._load(name)
            if not meta:
                return {"ok": False, "error": f"unknown place '{name}'", "places": self._names()}
            # Arrived? (the live view matches the saved reference closely enough)
            jpeg = await self._grab_jpeg(ctx)
            cur = _ahash(self._Image, jpeg) if (self._Image and jpeg) else None
            dist = _hamming(cur, meta["ahash"]) if (cur and meta.get("ahash")) else None
            if dist is not None and dist <= _ARRIVE_BITS:
                try:
                    await ctx.link.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._visited(name)
                return {"ok": True, "arrived": True, "place": name,
                        "note": f"Arrived at {name} (the view matches)."}
            # Not there yet: take ONE small, safety-clamped step toward it, then expect a re-check.
            step = await self._nav_step(ctx, meta)
            return {"ok": True, "arrived": False, "navigating_to": name, "view_distance": dist,
                    "has_reference": (PLACES_DIR / f"{name}.jpg").is_file(), "step": step,
                    "note": "Took a step toward it. Call go_to_place again (or look / where_am_i) to verify; "
                            "if the path looks blocked, turn first. Use dock instead if heading to the charger."}
        return h

    async def _nav_step(self, ctx: SkillContext, meta: dict) -> dict:
        """One bounded move toward a place: turn toward its bearing if we have pose, else nudge forward.
        Everything routes through the safety floor (autonomy/speed/duration), so it can't bypass limits."""
        target = meta.get("pose") or {}
        cur = self._pose(ctx)
        if target and cur:
            dx = float(target.get("x", 0.0)) - float(cur.get("x", 0.0))
            dy = float(target.get("y", 0.0)) - float(cur.get("y", 0.0))
            if math.hypot(dx, dy) > 1e-3:
                desired = math.degrees(math.atan2(dy, dx))
                err = ((desired - float(cur.get("yaw_deg", 0.0))) + 180.0) % 360.0 - 180.0
                if abs(err) > _TURN_TOLERANCE:
                    # err>0 => target is CCW (to the left) => turn left (rx<0 in the robot's convention).
                    return await self._drive(ctx, 0.0, -0.6 if err > 0 else 0.6, 0.5,
                                             f"turning {'left' if err > 0 else 'right'} toward target")
                return await self._drive(ctx, 0.6, 0.0, 1.0, "driving forward toward target")
        # No usable pose: a gentle forward nudge; the cortex steers visually across repeated calls.
        return await self._drive(ctx, 0.5, 0.0, 0.8, "forward (visual nav — no pose)")

    async def _drive(self, ctx: SkillContext, ly: float, rx: float, dur: float, desc: str) -> dict:
        d = ctx.safety.check_drive(ctx.settings, ly, rx, dur, source="ai")
        if not d.allowed:
            return {"moved": False, "blocked": d.reason}
        tk = ctx.safety.admit_motion()   # P0 §3: ticket go_to_place steps
        if tk is None:
            return {"moved": False, "blocked": "motion not admitted (STOP/latched)"}
        try:
            await ctx.link.move(d.ly, d.rx, d.duration, generation=tk.generation, epoch=tk.epoch,
                                ticket_id=tk.ticket_id)
        except Exception as e:  # noqa: BLE001
            return {"moved": False, "error": str(e)}
        return {"moved": True, "desc": desc, "ly": round(d.ly, 2), "rx": round(d.rx, 2), "duration": d.duration}

    async def _current_place(self, ctx: SkillContext) -> tuple[str | None, str | None]:
        """Best-effort 'where am I' as a (place_name, ahash) pair for tagging a thing's location."""
        jpeg = await self._grab_jpeg(ctx)
        cur = _ahash(self._Image, jpeg) if (self._Image and jpeg) else None
        if not cur:
            return None, None
        best, best_d = None, 64
        for name in self._names():
            h2 = (self._load(name) or {}).get("ahash")
            if h2:
                d = _hamming(cur, h2)
                if d < best_d:
                    best, best_d = name, d
        return (best if best is not None and best_d <= 12 else None), cur

    def _make_remember_thing(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            thing = str(a.get("thing", "")).strip()
            if not thing:
                return {"ok": False, "error": "thing required"}
            place, ahash = await self._current_place(ctx)
            rec = {"thing": thing, "place": place, "ahash": ahash, "ts": time.time(),
                   "note": str(a.get("note", "")).strip()}
            try:
                THINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
                with THINGS_PATH.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"save failed: {e}"}
            where = f"at {place}" if place else "here (unmapped spot)"
            ctx.memory.log_sighting(thing, kind="object", detail=(place or "unmapped"))
            return {"ok": True, "remembered": thing, "place": place, "note": f"noted {thing} {where}"}
        return h

    def _make_where_is(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            q = str(a.get("thing", "")).strip().lower()
            if not q:
                return {"ok": False, "error": "thing required"}
            recs = []
            try:
                if THINGS_PATH.exists():
                    for line in THINGS_PATH.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            r = json.loads(line)
                            if q in str(r.get("thing", "")).lower():
                                recs.append(r)
            except Exception:  # noqa: BLE001
                pass
            if not recs:
                return {"ok": True, "found": False, "note": f"I haven't noted where '{q}' is. I'll watch for it."}
            recs.sort(key=lambda r: r.get("ts", 0), reverse=True)
            latest = recs[-0:] if False else recs[:3]
            ago = lambda ts: f"{int((time.time() - ts) / 60)} min ago"  # noqa: E731
            sightings = [{"thing": r["thing"], "place": r.get("place") or "an unmapped spot",
                          "note": r.get("note", ""), "seen": ago(r.get("ts", time.time()))} for r in latest]
            best_place = next((r.get("place") for r in recs if r.get("place")), None)
            return {"ok": True, "found": True, "last_seen": sightings,
                    "go_hint": (f"Use go_to_place('{best_place}') to head there." if best_place else
                                "It was in an unmapped spot — explore to relocate it.")}
        return h

    def _make_where(self, ctx: SkillContext):
        async def h(a: dict) -> dict:
            if not self._Image:
                return {"ok": True, "match": None, "note": "visual match needs Pillow (pip install Pillow). "
                        "Describe the view from the camera instead."}
            jpeg = await self._grab_jpeg(ctx)
            cur = _ahash(self._Image, jpeg) if jpeg else None
            if not cur:
                return {"ok": False, "error": "no camera frame"}
            best, best_d = None, 64
            for name in self._names():
                meta = self._load(name) or {}
                h2 = meta.get("ahash")
                if h2:
                    d = _hamming(cur, h2)
                    if d < best_d:
                        best, best_d = name, d
            # <=12 bits different on an 8x8 hash ~ "plausibly the same view".
            match = best if best is not None and best_d <= 12 else None
            confidence = "high" if (match and best_d <= 6) else "low" if match else "none"
            # Confident recognition while roaming auto-builds the topological graph (prev place -> here).
            if match and confidence == "high":
                self._visited(match)
            return {"ok": True, "match": match, "distance": best_d if best else None,
                    "confidence": confidence}
        return h
