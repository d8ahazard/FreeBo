"""Voice command intents — the few spoken orders the robot ALWAYS respects.

A fast, flexible keyword/phrase matcher for the critical imperatives so they're honored instantly and
reliably (even mid-think). The cortex LLM still handles paraphrases and the long tail adaptively; this just
guarantees the important ones land. Order matters (more specific phrases first, e.g. "stop talking" -> QUIET
before plain "stop" -> STOP).

  STOP      — stop moving / halt / freeze            (always honored)
  QUIET     — shut up / be quiet / stop talking       (always honored)
  SLEEP     — go to sleep / power down / go dark       (always honored)
  SPEAK_UP  — I can't hear you / speak up / say again  (always honored)
  BACK_UP   — back up / you're stuck / reverse          (always honored)
  EXPLORE   — go explore / wander / look around          (owner-gated)
  HOME      — go home / dock / go charge                  (owner-gated)
  COME      — come here / come to me / follow me           (owner-gated)
"""
from __future__ import annotations

import re

# (name, always_honored, [phrase regexes]). Checked in order; first match wins.
_INTENTS: list[tuple[str, bool, list[str]]] = [
    ("QUIET",    True,  [r"shut\s*up", r"be\s*quiet", r"\bquiet\b", r"\bhush\b",
                         r"stop\s*(talking|speaking|chatting)", r"that'?s\s*enough", r"\benough\b"]),
    ("SPEAK_UP", True,  [r"can'?t\s*hear\s*you", r"cannot\s*hear\s*you", r"speak\s*up", r"\blouder\b",
                         r"say\s*(that|it)\s*again", r"what\s*did\s*you\s*say", r"come\s*again"]),
    ("BACK_UP",  True,  [r"back\s*up", r"\bback\s*off\b", r"you'?re\s*stuck", r"you\s*are\s*stuck",
                         r"\breverse\b", r"get\s*unstuck", r"you'?re\s*wedged"]),
    ("SLEEP",    True,  [r"go\s*to\s*sleep", r"go\s*dark", r"power\s*(down|off)", r"shut\s*(down|yourself)",
                         r"good\s*night", r"\bgoodnight\b", r"\bsleep\s*now\b", r"take\s*a\s*nap"]),
    ("STOP",     True,  [r"stop\s*(moving|driving|right there|now)?", r"\bhalt\b", r"\bfreeze\b",
                         r"\bwhoa\b", r"hold\s*(still|on|up)", r"don'?t\s*move", r"\bstay\b"]),
    ("HOME",     False, [r"go\s*home", r"\bgo\s*to\s*your\s*(dock|charger|home)\b", r"\bdock\b",
                         r"go\s*charge", r"charge\s*yourself", r"return\s*home", r"head\s*home"]),
    ("COME",     False, [r"come\s*here", r"come\s*over", r"over\s*here", r"come\s*to\s*me",
                         r"follow\s*me", r"\bcome\b"]),
    ("EXPLORE",  False, [r"go\s*explore", r"\bexplore\b", r"\bwander\b", r"go\s*roam", r"\broam\b",
                         r"look\s*around", r"go\s*look", r"check\s*(the\s*)?(house|place|rooms?)",
                         r"go\s*for\s*a\s*(roam|wander|walk)"]),
]

_COMPILED = [(name, always, [re.compile(p) for p in pats]) for name, always, pats in _INTENTS]

# Honored even from a non-owner when owner-only obedience is on (safety/comfort imperatives).
ALWAYS = {name for name, always, _ in _COMPILED if always}
# Applied immediately (low latency) rather than waiting for the reasoner to dequeue.
INSTANT = {"STOP", "QUIET", "SLEEP"}


def match(text: str) -> str | None:
    """Return the intent name for a spoken phrase, or None. First (most specific) match wins."""
    t = (text or "").lower().strip()
    if not t:
        return None
    for name, _always, pats in _COMPILED:
        if any(p.search(t) for p in pats):
            return name
    return None
