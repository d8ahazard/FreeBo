"""Daily memory cleanup / summarization.

Once a day, the HEAVY model (Settings.summarizer_model) reviews recent daily notes + current long-term
facts and distills them into a concise, deduplicated set of durable facts — the equivalent of a human
reviewing their journal and updating their long-term memory. This keeps the prompt-injected memory small
and high-signal while the fast model handles moment-to-moment interaction.

Fail-safe: if the model errors or returns junk, we keep the existing facts untouched (never destroy memory
on a bad summarization).
"""
from __future__ import annotations

import json

from ..config import Settings
from .memory import Fact, Memory
from .providers import OpenAICompatibleClient, ProviderError

_PROMPT = """You are the long-term memory keeper for a companion robot named {name}.
Review the robot's CURRENT long-term facts and its RECENT daily notes, then return the CLEANED long-term
memory: a concise, deduplicated set of durable facts worth keeping (owner identity & preferences, people,
pets, places, recurring routines, important events). Drop trivia, transient observations, and duplicates.
Merge related items. Keep it under ~40 items.

Return ONLY a JSON array of objects: [{{"text": "...", "kind": "fact|preference|person|place|event"}}].

CURRENT FACTS:
{facts}

RECENT DAILY NOTES:
{notes}
"""


async def summarize(settings: Settings, memory: Memory) -> dict:
    s = settings.snapshot()
    facts = memory.all_facts()
    notes = memory.recent_events(days=3)
    if not facts and not notes:
        return {"ok": True, "skipped": "nothing to summarize"}

    prompt = _PROMPT.format(
        name=s.robot_name or "Autobot",
        facts="\n".join(f"- ({f.kind}) {f.text}" for f in facts) or "(none)",
        notes="\n".join(f"- {n}" for n in notes[-200:]) or "(none)",
    )
    client = OpenAICompatibleClient(s.ai_base_url, s.ai_api_key, s.summarizer_model())
    try:
        result = await client.chat([{"role": "user", "content": prompt}])
    except ProviderError as e:
        memory.log_event(f"daily summarization failed: {e}", source="system")
        return {"ok": False, "error": str(e)}

    cleaned = _parse_facts(result.content)
    if cleaned is None:
        memory.log_event("daily summarization returned no parseable facts; keeping existing", source="system")
        return {"ok": False, "error": "no parseable facts"}

    memory.replace_facts(cleaned)
    pruned = memory.prune_daily()   # old raw notes are now distilled into facts.json — safe to drop
    memory.log_event(f"daily memory cleanup: {len(facts)} -> {len(cleaned)} facts (heavy model "
                     f"{s.summarizer_model()}); pruned {pruned} old daily files", source="system")
    return {"ok": True, "before": len(facts), "after": len(cleaned), "pruned_daily": pruned}


def _parse_facts(content: str) -> list[Fact] | None:
    if not content:
        return None
    txt = content.strip()
    # tolerate code fences / surrounding prose by extracting the first JSON array
    start, end = txt.find("["), txt.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(txt[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    out: list[Fact] = []
    for d in data:
        if isinstance(d, dict) and str(d.get("text", "")).strip():
            out.append(Fact(text=str(d["text"]).strip(), kind=str(d.get("kind", "fact")), source="summary"))
    return out or None
