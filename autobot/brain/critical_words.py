"""Critical (emergency) command words for live barge-in during TTS playback.

This is deliberately NARROW — only STOP and QUIET. The general voice-command matcher in `commands.py` has broad,
conversational phrases ("that's enough", "hold on") that would false-fire against the robot's own TTS and room
echo while it is speaking. Barge-in runs in exactly that adversarial condition, so it gets its own tight set.

Two halves:
  * `match_barge_in(text)` — final classification stage (text -> STOP|QUIET|None). The PCM->text step happens
    upstream in AudioSink's bounded barge-in worker; a regex cannot run on PCM.
  * `strip_reserved(text)` — sanitize OUTBOUND TTS so the robot never says a barge-in trigger word itself
    (defence in depth: even with self-echo rejection, the cleanest fix is to not utter the trigger at all).
"""
from __future__ import annotations

import re

# Tight, anchored patterns. STOP and QUIET only — the two imperatives that must land mid-speech.
BARGE_IN_PATTERNS: dict[str, list[str]] = {
    "STOP": [r"\bstop\b", r"\bhalt\b", r"\bfreeze\b"],
    "QUIET": [r"\bquiet\b", r"shut\s*up", r"be\s*quiet", r"stop\s*talking"],
}
_COMPILED = [(name, [re.compile(p, re.I) for p in pats]) for name, pats in BARGE_IN_PATTERNS.items()]

# Outbound TTS sanitization: map any trigger token to a safe synonym that is NOT in the barge-in set, so the
# robot's own speech can never transcribe back into a STOP/QUIET self-trigger. Order matters (phrases first).
_SANITIZE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bshut\s*up\b", re.I), "hush"),
    (re.compile(r"\bbe\s*quiet\b", re.I), "settle down"),
    (re.compile(r"\bstop\s*talking\b", re.I), "wrap up"),
    (re.compile(r"\bstop\b", re.I), "hold up"),
    (re.compile(r"\bhalt\b", re.I), "hold"),
    (re.compile(r"\bfreeze\b", re.I), "hold still"),
    (re.compile(r"\bquiet\b", re.I), "calm"),
]


def match_barge_in(text: str) -> str | None:
    """Return 'STOP' | 'QUIET' for an emergency phrase heard during playback, else None. STOP wins ties."""
    t = (text or "").lower().strip()
    if not t:
        return None
    for name, pats in _COMPILED:
        if any(p.search(t) for p in pats):
            return name
    return None


def strip_reserved(text: str) -> str:
    """Rephrase any barge-in trigger word out of outbound TTS so the robot never self-triggers a STOP/QUIET."""
    out = text or ""
    for pat, repl in _SANITIZE:
        out = pat.sub(repl, out)
    return out


def is_self_echo(heard: str, tts_text: str) -> bool:
    """True if `heard` looks like our own in-flight TTS bleeding back into the mic (no hardware AEC on this
    path). Conservative: a short heard phrase whose words are all contained in the TTS text is likely echo."""
    h = re.sub(r"[^a-z0-9 ]", " ", (heard or "").lower()).split()
    if not h:
        return True
    tts = set(re.sub(r"[^a-z0-9 ]", " ", (tts_text or "").lower()).split())
    if not tts:
        return False
    return all(w in tts for w in h)
