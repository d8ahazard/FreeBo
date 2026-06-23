"""Curiosity / anti-repetition engine — what stops the robot roaming in circles and narrating the same wall.

A small, dependency-light signal the cortex reads each cycle. It watches two things:

  * SCENE NOVELTY — how different the current view (the VLM's scene description) is from what it has been
    seeing lately. When the view stops changing, "boredom" rises and we nudge the brain to go find something
    new (a different direction, a doorway, another room).
  * ACTION REPETITION — when the same move is chosen over and over, we nudge the brain to vary it.

Phase 3 layers spatial coverage on top via `note_position` / `coverage_hint` (least-visited heading), so the
nudge can be concrete ("you've been in this spot — try heading left toward unexplored space"). Everything is
stdlib only and fail-soft; it never blocks or breaks the loop. The cortex is still free to ignore it.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter, deque

# Jaccard similarity above this = "basically the same scene as before".
_SAME_SCENE = 0.6
# Boredom climbs by 1 per stale look; nudge the brain once it crosses this.
_BORED_NUDGE = 3
_BORED_MAX = 8
# Stopwords stripped before comparing scene descriptions, so boredom tracks *content* not boilerplate.
_STOP = {
    "the", "a", "an", "is", "are", "of", "in", "on", "to", "and", "or", "with", "at", "it", "this", "that",
    "there", "here", "you", "your", "see", "scene", "objects", "people", "paths", "none", "nothing",
    "notable", "ahead", "left", "right", "center", "floor", "open", "room", "space", "robot", "camera",
    "view", "front", "appears", "looks", "some", "be", "no", "i", "can", "what", "where",
}


def _tokens(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z]{3,}", (text or "").lower())
    return frozenset(w for w in words if w not in _STOP)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class Curiosity:
    """Tracks scene novelty + action repetition and turns them into a short prompt nudge."""

    def __init__(self, scene_window: int = 6, action_window: int = 8, cell_size: float = 0.3):
        self._scenes: deque[frozenset[str]] = deque(maxlen=scene_window)
        self._actions: deque[str] = deque(maxlen=action_window)
        self.boredom: float = 0.0
        self.last_novelty: float = 1.0
        self._coverage_hint: str = ""     # least-visited heading, derived from the spatial coverage grid
        self._last_scene_ts: float = 0.0
        # Spatial coverage: a rough visited-cell histogram in VSLAM's (up-to-scale) world frame.
        self._cells: Counter = Counter()
        self._cell_size = cell_size

    # --- inputs ---
    def note_scene(self, caption: str) -> float:
        """Record the latest scene description; returns its novelty (0=identical to recent, 1=brand new)."""
        toks = _tokens(caption)
        if not toks:
            return self.last_novelty
        sim = max((_jaccard(toks, prev) for prev in self._scenes), default=0.0)
        novelty = 1.0 - sim
        self._scenes.append(toks)
        self.last_novelty = novelty
        self._last_scene_ts = time.time()
        if sim >= _SAME_SCENE:
            self.boredom = min(_BORED_MAX, self.boredom + 1)
        else:
            self.boredom = max(0.0, self.boredom - 2)   # genuinely new sight = relief from boredom
        return novelty

    def note_action(self, direction: str) -> None:
        d = (direction or "").strip().lower()
        if d and d not in ("stop", "none", "wait", "look"):
            self._actions.append(d)

    def note_position(self, x: float, y: float, yaw_deg: float) -> None:
        """Record a visit to the robot's current (rough VSLAM) cell and derive a least-visited heading hint.

        Pose is monocular + up-to-scale + drifty, so this is intentionally coarse: it only tells the brain
        which RELATIVE direction (forward/left/right/back) currently points at the least-visited neighbour."""
        cx, cy = round(x / self._cell_size), round(y / self._cell_size)
        self._cells[(cx, cy)] += 1
        yaw = math.radians(yaw_deg)
        # robot-relative bearings: left is +90 deg (CCW), right -90, back 180 (matches VSLAM yaw convention).
        rel = {"ahead": 0.0, "left": math.pi / 2, "right": -math.pi / 2, "behind you": math.pi}
        best_label, best_count = "", None
        for label, off in rel.items():
            a = yaw + off
            nx = round((x + math.cos(a) * self._cell_size) / self._cell_size)
            ny = round((y + math.sin(a) * self._cell_size) / self._cell_size)
            c = self._cells.get((nx, ny), 0)
            if best_count is None or c < best_count:
                best_count, best_label = c, label
        # Only nudge when there's a genuinely unexplored neighbour; otherwise stay quiet (don't over-steer).
        self._coverage_hint = (f"less-explored space seems to be {best_label}"
                               if best_label and best_count == 0 else "")

    # --- outputs ---
    def is_bored(self) -> bool:
        return self.boredom >= _BORED_NUDGE

    def _repeated_action(self) -> str | None:
        if len(self._actions) < 4:
            return None
        dirn, n = Counter(self._actions).most_common(1)[0]
        return dirn if n >= max(4, len(self._actions) * 3 // 4) else None

    def prompt_fragment(self) -> str:
        lines: list[str] = []
        if self.is_bored():
            msg = ("CURIOSITY: your view hasn't changed in a while — you're lingering in one spot. Be a "
                   "curious explorer: pick a NEW direction, head toward a doorway or open space, and go find "
                   "something you haven't seen yet.")
            if self._coverage_hint:
                msg += f" ({self._coverage_hint})"
            lines.append(msg)
        rep = self._repeated_action()
        if rep:
            lines.append(f"CURIOSITY: you've chosen '{rep}' several times in a row — vary your movement so "
                         f"you actually cover new ground instead of circling.")
        return "\n".join(lines)

    def state(self) -> dict:
        return {"boredom": round(self.boredom, 1), "novelty": round(self.last_novelty, 2),
                "bored": self.is_bored(), "coverage_hint": self._coverage_hint}
