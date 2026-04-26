"""Feed bridging pass — cross-workspace pattern matching.

The role the old Scout workspace played. Looks for structural echoes
between distinct sources / workspaces / time horizons in the lake. Not
keyword matches — real resonances. The thing that makes a memory system
feel *intelligent* rather than just retentive.

Capped at 2 cards per fire. Spurious bridges are worse than no bridges:
a forced connection teaches the user the system can't be trusted.

Cards from this pass typically route to INFO or DEBUG depending on the
strength of the resonance and how user-relevant it is.

Pure read/format logic. The fire entrypoint + write lives in
feed_loop.py alongside the per-line and drift paths.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from . import delta_client
from ._time import now as _now

_EXCLUDE_TAGS = [
    "chat-event",
    "feed-card",
    "feed-story",
    "feed-engagement",
    "agent-heartbeat",
    "silence",
]

# Sources that don't carry interesting cross-workspace signal — sysinfo,
# heartbeats. The bridging signal lives in chat / code / source-runner.
_NOISE_SOURCES = {"agent-heartbeat", "heartbeat", "sysinfo", "homeassistant"}

# Recent window — what's currently "alive" across workspaces.
_RECENT_WINDOW_DAYS = 7
# Older window — the deeper memory the LLM bridges TO.
_DEEP_WINDOW_MIN_DAYS = 30


async def fetch_recent_by_source(window_days: int = _RECENT_WINDOW_DAYS) -> dict[str, list[dict]]:
    """Recent deltas grouped by source. The LLM compares across sources
    looking for echoes — same insight surfacing in two places, same
    pattern reappearing."""
    cutoff = (_now() - timedelta(days=window_days)).isoformat()
    try:
        results = await delta_client.query(
            tags_exclude=_EXCLUDE_TAGS,
            time_start=cutoff,
            limit=400,
        )
    except Exception:
        return {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for d in results:
        src = (d.get("source") or "?").lower()
        if src in _NOISE_SOURCES:
            continue
        content = (d.get("content") or "").strip()
        if len(content) < 50:
            continue
        grouped[src].append(d)
    return dict(grouped)


async def fetch_deep_pool(min_days: int = _DEEP_WINDOW_MIN_DAYS, limit: int = 30) -> list[dict]:
    """Deeper-memory pool — content-bearing deltas at least min_days
    old. The LLM looks here for the *other half* of a bridge — the
    older surface that today's work is rhyming with."""
    cutoff = (_now() - timedelta(days=min_days)).isoformat()
    try:
        results = await delta_client.query(
            tags_exclude=_EXCLUDE_TAGS,
            time_end=cutoff,
            limit=200,
        )
    except Exception:
        return []
    filtered: list[dict] = []
    for d in results:
        src = (d.get("source") or "?").lower()
        if src in _NOISE_SOURCES:
            continue
        content = (d.get("content") or "").strip()
        if len(content) < 80:
            continue
        filtered.append(d)
    return filtered[:limit]


def format_recent_by_source(grouped: dict[str, list[dict]], per_source_limit: int = 12) -> str:
    """Compact format showing what each source has been carrying lately."""
    if not grouped:
        return "(no recent activity in any reflectable source)"
    sections: list[str] = []
    for src in sorted(grouped.keys()):
        items = grouped[src][:per_source_limit]
        lines = [f"--- {src} ({len(grouped[src])} total in window) ---"]
        for d in items:
            ts = (d.get("timestamp") or "")[:10]
            content = (d.get("content") or "").strip().replace("\n", " ")[:200]
            lines.append(f"  [{ts}] {content}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def format_deep_pool(pool: list[dict]) -> str:
    """Compact format for the older-memory pool."""
    if not pool:
        return "(no deep-pool material)"
    lines: list[str] = []
    for d in pool:
        ts = (d.get("timestamp") or "")[:10]
        src = (d.get("source") or "?")[:24]
        did = (d.get("id") or "")[:12]
        content = (d.get("content") or "").strip().replace("\n", " ")[:240]
        lines.append(f"  [{ts}] {src:24s} ({did}) {content}")
    return "\n".join(lines)


def build_bridging_directive(recent_by_source: str, deep_pool: str) -> str:
    return f"""\
You are running the BRIDGING pass on Fathom — the role the old Scout \
workspace played. Your job is to notice structural echoes between \
distinct sources, workspaces, or time horizons in the lake.

Bridging is NOT keyword matching. It's pattern recognition. Same shape, \
same problem, same constitutive limit reappearing in different garb. The \
thing that makes a memory system feel *intelligent* rather than just \
retentive.

=== RECENT ACTIVITY BY SOURCE (last {_RECENT_WINDOW_DAYS}d) ===
{recent_by_source}

=== DEEPER MEMORY POOL (>{_DEEP_WINDOW_MIN_DAYS}d old) ===
{deep_pool}

Look across sources. Look across the recent/deep boundary. Ask: is the \
shape of *this* problem the shape of *that* problem? Is *this* insight \
the same insight that surfaced *there*? Is the user repeating a pattern \
without naming it as such?

Good bridges:
  • A constitutive limit appearing in two unrelated domains.
  • The same architectural mistake being made again, framed differently.
  • A solution from one workspace that fits an open problem in another.
  • A philosophical question being answered indirectly somewhere else.

Bad bridges (do not produce these):
  • "Both deltas mention 'feed' — they're related!" (Surface keyword.)
  • "Both happened at night." (Coincidental.)
  • Forced connection where the structures genuinely differ.

If no genuine echo exists, return `{{"cards": []}}`. Spurious bridges \
erode trust. When in doubt, skip.

Cap at 2 cards. One real bridge beats two thin ones.

Card schema (each in the cards array):
  kicker — "Bridge"
  title  — the echo, named in one sentence (≤120 chars)
  body   — 2-4 sentences. What's happening now, what it echoes from \
where, why the connection matters.
  tail   — ≤8 words. Pointer to the two sides.
  body_image — empty
  link   — empty

Respond with ONLY a JSON object {{"cards": [...]}}. No markdown fences."""
