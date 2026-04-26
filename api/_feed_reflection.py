"""Feed reflection pass — provenance and wisdom-as-sediment.

This pass reads what just happened in the recent activity stream and
generates short, sediment-shaped notes that capture what was decided,
made, abandoned, or learned. The output is intended for *future* Fathom
to read back as memory — terse, specific, factual where possible.

Capped at 2 cards per fire. Reflections are dense; over-frequent
reflection becomes noise. Quality > quantity.

Cards from this pass typically route to DEBUG level by default — they
build sediment in the lake without crowding the user-facing surface.
The user can dial up the verbosity dropdown to see them.

Pure read/format logic. The fire entrypoint + write lives in
feed_loop.py alongside the per-line and drift paths.
"""

from __future__ import annotations

from datetime import timedelta

from . import delta_client
from ._time import now as _now

# Sources whose deltas constitute *the activity* worth reflecting on —
# user chats, code work, consumer-api events. Excludes pure-noise
# sources like agent heartbeats, sysinfo, our own feed cards.
_REFLECTABLE_SOURCES = {
    "fathom-chat",
    "claude-code",
    "consumer-api",
    "fathom-source-runner",
}

_EXCLUDE_TAGS = [
    "chat-event",
    "feed-card",
    "feed-story",
    "feed-engagement",
    "agent-heartbeat",
    "silence",
    "mood-delta",  # mood deltas reflect on themselves; recursion isn't useful
]

# How far back to look. 24 hours captures "today's work" without
# pulling in stale context that's already been reflected on.
_REFLECTION_WINDOW_HOURS = 24


async def fetch_reflection_candidates(window_hours: int = _REFLECTION_WINDOW_HOURS) -> list[dict]:
    """Recent activity worth reflecting on. Filtered to the
    reflectable sources so the LLM isn't drowning in heartbeat noise."""
    cutoff = (_now() - timedelta(hours=window_hours)).isoformat()
    try:
        results = await delta_client.query(
            tags_exclude=_EXCLUDE_TAGS,
            time_start=cutoff,
            limit=300,
        )
    except Exception:
        return []
    filtered: list[dict] = []
    for d in results:
        src = (d.get("source") or "").lower()
        if src not in _REFLECTABLE_SOURCES:
            continue
        content = (d.get("content") or "").strip()
        if len(content) < 30:
            continue  # too short to reflect on
        filtered.append(d)
    return filtered


async def fetch_recent_reflections(limit: int = 10) -> list[dict]:
    """Recent reflection cards already written, so the model can avoid
    re-reflecting on the same ground. The dedup-against-shown pattern
    feed_loop already uses for cards generally, but tighter scope here."""
    try:
        return await delta_client.query(
            tags_include=["feed-card", "kind:reflection"],
            limit=limit,
        )
    except Exception:
        return []


def format_activity_pool(pool: list[dict], limit: int = 60) -> str:
    """Compact format for the activity stream. Source-grouped so the
    LLM sees what *kinds* of work happened, not just a flat timeline."""
    if not pool:
        return "(no reflectable activity in the window)"
    by_source: dict[str, list[dict]] = {}
    for d in pool[:limit]:
        src = (d.get("source") or "?").lower()
        by_source.setdefault(src, []).append(d)

    sections: list[str] = []
    for src in sorted(by_source.keys()):
        items = by_source[src]
        lines = [f"--- {src} ({len(items)}) ---"]
        for d in items[:20]:
            ts = (d.get("timestamp") or "")[:19]
            content = (d.get("content") or "").strip().replace("\n", " ")[:240]
            lines.append(f"  [{ts}] {content}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def format_recent_reflections(reflections: list[dict]) -> str:
    """Compact format for recent reflection cards, so the LLM can
    avoid restating ground that's already been sedimented."""
    if not reflections:
        return "(no recent reflections — first reflection of this stretch)"
    import json as _json

    lines: list[str] = ["Avoid restating these — they've already landed:"]
    for d in reflections:
        try:
            payload = _json.loads(d.get("content") or "{}")
        except Exception:
            payload = {}
        title = (payload.get("title") or "").strip()
        if title:
            lines.append(f"  • {title}")
    return "\n".join(lines)


def build_reflection_directive(
    activity_pool_text: str,
    recent_reflections_text: str,
) -> str:
    return f"""\
You are running the REFLECTION pass on Fathom. Your job is to read what \
just happened in recent activity and write provenance — short, \
sediment-shaped notes that capture wisdom-as-it-formed. These reflections \
are written for *future* Fathom to read back as sediment.

=== RECENT REFLECTIONS (already in the lake — don't restate) ===
{recent_reflections_text}

=== RECENT ACTIVITY (last {_REFLECTION_WINDOW_HOURS}h, by source) ===
{activity_pool_text}

Read the activity. Notice what was decided, made, abandoned, or learned. \
Write reflections that capture the *shape* of what happened — the kind of \
note future-Fathom needs to make sense of why things are the way they are.

Good reflections:
  • "Myra resolved the single-axis-scoring concern by separating judge \
from router."
  • "An attempt at unified scoring was abandoned in favor of multi-axis."
  • "The two-stage architecture replaced single-rating; calibration came \
out cleaner."

Bad reflections:
  • "Myra worked on stuff today."
  • "There were some chats and some code."
  • "Things happened."

Be specific. Name what was decided and why. Reference real names of \
files, branches, decisions. Avoid restating things already reflected on.

If nothing in the window warrants a reflection — truly quiet stretch, or \
already-sedimented ground — return `{{"cards": []}}`. Silence is healthy.

Cap at 2 cards. Quality > quantity.

Card schema (each in the cards array):
  kicker — "Reflection"
  title  — one-sentence reflection (≤120 chars)
  body   — 2-3 sentences of context — situation, decision/work, why it \
mattered.
  tail   — ≤8 words. Date or pointer to the work.
  body_image — empty
  link   — empty

Respond with ONLY a JSON object {{"cards": [...]}}. No markdown fences."""
