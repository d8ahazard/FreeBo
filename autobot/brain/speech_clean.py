"""Sanitize text the robot is about to SPEAK.

Small local cortex models (e.g. qwen2.5:7b via Ollama) are unreliable at the tool-call boundary: they often
pack a little action-script INTO the `say` text, e.g.

    set_eyes "curious"
    say "I'm here, owner! To my left I see folding chairs."

or prefix a reply with `Say:` / `say:`. Speaking that verbatim is the visible "Say: '...'" bug. This module
extracts just the human sentence(s): unwrap `say("...")`, drop lines that are really mislabeled tool calls,
strip a leading tool keyword, and de-quote. It is the single chokepoint used by both the `say` skill and the
agent's plain-chat speak path, so no spoken text bypasses it.
"""
from __future__ import annotations

import re

# Tool/keyword names the cortex sometimes emits as leading tokens of a line when it means an ACTION, not
# speech. A line starting with one of these (and not a genuine sentence) is dropped from spoken output.
_TOOL_WORDS = {
    "say", "set_eyes", "eyes", "drive", "move", "stop", "look", "turn", "remember", "recall", "forget",
    "dock", "undock", "set_behavior", "go_to_place", "patrol", "action", "eyes:",
}

# `say "..."` / `say: '...'` / `say(...)` wrappers — pull out the quoted payload(s).
_SAY_WRAP = re.compile(r"""\bsay\s*[:(]?\s*(["'])(.+?)\1""", re.S | re.I)
_LEAD_TOOL = re.compile(r"^([A-Za-z_]\w*)\b", re.I)
_LEAD_PREFIX = re.compile(r"^[A-Za-z_]\w*\s*:?\s*", re.I)


def clean_spoken(text: str) -> str:
    """Return only the natural-language sentence(s) safe to vocalize; '' if nothing real remains."""
    if not text:
        return ""
    t = text.strip()
    # Case 1: the payload is one or more `say "..."` wrappers (often with other tool lines around them).
    wraps = _SAY_WRAP.findall(t)
    if wraps:
        joined = " ".join(w[1].strip() for w in wraps if w[1].strip())
        return joined.strip().strip("\"'")[:300]
    # Case 2: line-by-line — drop lines that are really mislabeled tool calls, keep prose.
    kept: list[str] = []
    for line in t.splitlines():
        ln = line.strip()
        if not ln:
            continue
        m = _LEAD_TOOL.match(ln)
        if m and m.group(1).lower() in _TOOL_WORDS:
            # A `say <rest>` line contributes its remainder as speech; other tool lines are dropped entirely.
            if m.group(1).lower() == "say":
                rest = _LEAD_PREFIX.sub("", ln).strip().strip("\"'")
                if rest:
                    kept.append(rest)
            continue
        kept.append(ln)
    return " ".join(kept).strip().strip("\"'")[:300]
