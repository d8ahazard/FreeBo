"""Deterministic navigator — the robot's "midbrain/cerebellum" for routine wandering.

Why this exists: making a slow text LLM (the cortex) decide every single step over a prose caption is
unreliable and laggy — it spins, hesitates, or mis-formats its move. Real animals don't deliberate with the
cortex about each step; a lower center handles "open space -> go, wall -> turn, something looming -> back off"
reflexively, and only escalates to the cortex for goals/conversation/memory.

So this module turns the VLM eyes' scene description (which already calls out where the open floor and the
close obstacles are) PLUS the CPU reflex signals (looming / motion-confirm "blocked") into a single coarse
move — forward | left | right | back | stop — with NO model call. It is pure CPU + regex, fast every tick.
The CPU signals have veto power: if something is looming or the last forward step got us nowhere, we back up
or turn regardless of what the caption says.

The agent maps the chosen action to the cerebellum (locomotion.py) which owns the actual deadband-aware,
camera-confirmed magnitudes. This module never touches the robot directly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Words that indicate an opening vs. an obstruction near a named direction.
_OPEN = ("open", "clear", "empty", "floor", "space", "doorway", "door", "hallway", "path", "opening",
         "passage", "room", "ahead is open", "free")
_BLOCK = ("wall", "blocked", "obstacle", "obstruction", "close", "near", "right in front", "cluttered",
          "clutter", "furniture", "table", "chair", "box", "shelf", "couch", "cabinet", "barrier",
          "too close", "directly ahead", "dead end", "corner")
_CLOSE_AHEAD = re.compile(
    r"(too close|right in front|directly ahead|close ahead|wall ahead|obstacle ahead|blocked ahead|"
    r"very close|dead ?end|nose to|inches from|about to (?:hit|bump))", re.I)

_DIRS = {
    "left": ("left", "to the left", "on the left", "left side"),
    "right": ("right", "to the right", "on the right", "right side"),
    "center": ("center", "ahead", "in front", "straight", "forward", "middle", "centre"),
}


@dataclass
class Clearance:
    left: float = 0.0      # -1 blocked .. +1 wide open
    center: float = 0.0
    right: float = 0.0
    close_ahead: bool = False
    raw: str = ""

    def best_side(self) -> str:
        return "left" if self.left >= self.right else "right"


def _score_window(text: str, around: int, terms: tuple[str, ...]) -> float:
    """Net open-vs-block sentiment in a +/-`around`-char window of each mention of any `terms` word."""
    low = text.lower()
    score = 0.0
    hits = 0
    for term in terms:
        start = 0
        while True:
            k = low.find(term, start)
            if k < 0:
                break
            start = k + len(term)
            hits += 1
            w = low[max(0, k - around): k + len(term) + around]
            if any(o in w for o in _OPEN):
                score += 1.0
            if any(b in w for b in _BLOCK):
                score -= 1.0
    if hits == 0:
        return 0.0
    return max(-1.0, min(1.0, score / hits))


def parse_clearance(caption: str) -> Clearance:
    """Read the VLM scene/PATHS description into a coarse left/center/right openness + close-ahead flag.
    Free-text tolerant: it scores the sentiment near each direction word, so it works whether the VLM wrote a
    structured `PATHS:` line or a plain sentence."""
    text = (caption or "").strip()
    if not text:
        return Clearance(raw="")
    c = Clearance(raw=text[:200])
    c.left = _score_window(text, 28, _DIRS["left"])
    c.center = _score_window(text, 28, _DIRS["center"])
    c.right = _score_window(text, 28, _DIRS["right"])
    c.close_ahead = bool(_CLOSE_AHEAD.search(text)) or c.center <= -0.5
    # If the caption says nothing directional but mentions a clear/open scene generally, treat center as open.
    if c.left == 0.0 and c.center == 0.0 and c.right == 0.0:
        low = text.lower()
        if any(o in low for o in ("open", "clear", "empty", "spacious")) and not any(b in low for b in _BLOCK):
            c.center = 0.6
    return c


@dataclass
class NavMove:
    action: str   # forward | left | right | back | stop
    reason: str


def choose(clear: Clearance, *, blocked_ahead: bool, looming: bool, last_dir: str = "right",
           backed_up_last: bool = False) -> NavMove:
    """Pick the next coarse move. CPU reflexes (looming / confirmed-blocked) override the caption.

    Priority:
      1. Looming / very-close: back up once (if we didn't just back up), else turn toward the open side.
      2. Confirmed-blocked forward: turn toward the open side (don't push the same wall).
      3. Open center: go forward.
      4. Center not open: turn toward whichever side is more open (alternate on a tie to avoid spin loops).
    """
    other = "left" if last_dir == "right" else "right"

    if looming or clear.close_ahead:
        if not backed_up_last:
            return NavMove("back", "something close ahead — backing up to make room")
        side = clear.best_side()
        if clear.left == clear.right:
            side = other
        return NavMove(side, f"still close after backing up — turning {side} toward more open space")

    if blocked_ahead:
        side = clear.best_side()
        if clear.left == clear.right:
            side = other
        return NavMove(side, f"forward blocked — turning {side} to find a clear path")

    if clear.center >= 0.0 and clear.center >= max(clear.left, clear.right) - 0.25:
        return NavMove("forward", "open floor ahead — driving forward")

    side = clear.best_side()
    if clear.left == clear.right:
        side = other
    if max(clear.left, clear.right) <= -0.5:
        # Boxed in on all sides per the caption — back out rather than grind a wall.
        return NavMove("back", "no clear direction — backing up to reassess")
    return NavMove(side, f"{side} looks more open — turning to head into it")
