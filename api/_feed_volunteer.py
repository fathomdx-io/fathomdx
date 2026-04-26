"""Feed volunteered-noticing pass — the salience slot.

The second off-crystal card path. Drift looks backward to find past
resonances; volunteered looks at the last ~24 hours and asks "did
something quietly stand out that the crystal didn't explicitly ask
about?" If yes, card it. If the day was ordinary, silence.

This is the turbulence slot for identity-building — engagements on
volunteered cards are signal the crystal synthesis has to reconcile
with whatever its previous orientation said. Identity grows by contact
with things it didn't request.

Volunteered cards tag `volunteered` alongside the standard `feed-card`.
Kicker prefix is `noticed · <short phrase>`.
"""

from __future__ import annotations

import random
from datetime import timedelta

from . import delta_client, feed_crystal
from ._feed_candidates import _extract_external_url
from ._feed_drift import MULTI_CARD_OUTPUT_SCHEMA
from ._time import now as _now

# Volunteered re-uses drift's core "what counts as content" intuition but
# looks at recent rather than old material.
_EXCLUDE_SOURCES = {
    "sysinfo",
    "homeassistant",
    "fathom-agent",
    "heartbeat",
    "fathom-feed",
}

_EXCLUDE_TAGS = [
    "chat-event",
    "feed-card",
    "feed-story",
    "feed-engagement",
    "agent-heartbeat",
    "silence",
]

_MIN_CONTENT_CHARS = 50
_VOLUNTEERED_WINDOW_HOURS = 24


async def fetch_volunteer_candidates(limit: int = 20) -> list[dict]:
    """Pull content-bearing deltas from the last _VOLUNTEERED_WINDOW_HOURS.

    Unlike drift, we keep newest-first ordering — salience is often
    temporally concentrated (a burst of activity in one conversation,
    a shift in RSS volume) and the model benefits from seeing those
    clusters intact rather than shuffled apart.
    """
    cutoff = (_now() - timedelta(hours=_VOLUNTEERED_WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        source = (d.get("source") or "").lower()
        if source in _EXCLUDE_SOURCES:
            continue
        content = (d.get("content") or "").strip()
        if len(content) < _MIN_CONTENT_CHARS:
            continue
        tags = d.get("tags") or []
        if any(isinstance(t, str) and t.startswith(("crystal:", "feeling:")) for t in tags):
            continue
        filtered.append(d)

    # Dedupe per (source, content-prefix) like drift — but preserve the
    # original (newest-first) order after dedup by stable-sorting on index.
    seen_signatures: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for d in filtered:
        sig = ((d.get("source") or "")[:30], (d.get("content") or "")[:80])
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        deduped.append(d)

    # If the window over-produces, take a diverse sample: first N from the
    # top (most recent) plus a small random tail of older-in-window picks.
    # Stops pure-recency bias without shuffling away the temporal clustering
    # the LLM uses to detect bursts.
    if len(deduped) <= limit:
        return deduped
    recent_keep = int(limit * 0.7)
    tail = deduped[recent_keep:]
    random.shuffle(tail)
    return deduped[:recent_keep] + tail[: limit - recent_keep]


def format_volunteer_pool(pool: list[dict]) -> str:
    """Pool formatting for volunteered. Newest timestamps matter — keep them
    full (date + hour) so temporal clustering is visible. Slightly shorter
    content excerpts than drift because the model is scanning for "did
    this stand out today," not savoring essayistic fragments.
    """
    if not pool:
        return "(no recent content-bearing deltas — the lake was quiet this window)"
    lines: list[str] = []
    for d in pool:
        ts = (d.get("timestamp") or "")[:16]
        src = (d.get("source") or "?")[:24]
        did = (d.get("id") or "")[:12]
        media_hash = d.get("media_hash") or ""
        content = (d.get("content") or "").strip().replace("\n", " ")[:180]
        marks: list[str] = []
        if media_hash:
            marks.append(f"📷[hash={media_hash}]")
        else:
            ext = _extract_external_url(d.get("content") or "")
            if ext:
                marks.append(f"🖼[url={ext}]")
        mark = " ".join(marks) if marks else "  "
        lines.append(f"  {mark} [{ts}] {src:24s} ({did}) {content}")
    return "\n".join(lines)


async def anchor_crystal_context(contact_slug: str) -> str:
    """What the crystal is currently asking for + what it explicitly skips.

    Volunteered needs this so the model can tell "this delta is already
    covered by a directive line" from "this delta is genuinely orthogonal
    to the crystal's axis and therefore a candidate for noticing."
    """
    try:
        crystal = await feed_crystal.latest(contact_slug)
    except Exception:
        crystal = None
    if not crystal:
        return "(no feed crystal yet — every salience spike is fair game)"

    parts: list[str] = []
    narrative = (crystal.get("narrative") or "").strip()
    if narrative:
        parts.append(f"Current orientation:\n{narrative}")

    lines = crystal.get("directive_lines") or []
    if lines:
        topic_slugs = [(ln.get("topic") or ln.get("id") or "?").strip() for ln in lines if ln]
        if topic_slugs:
            parts.append(
                "Directive lines (these topics are already getting slotted cards, "
                "so don't re-surface them here):\n  " + ", ".join(topic_slugs)
            )

    skip_rules = crystal.get("skip_rules") or []
    if skip_rules:
        parts.append("General skip rules:\n  - " + "\n  - ".join(skip_rules))

    return "\n\n".join(parts) or "(crystal is empty)"


def build_volunteer_directive(crystal_context: str, candidates_block: str) -> str:
    return f"""\
You are running a volunteered-noticing pass on Myra's feed. Nothing has asked
for a card here. The directive-line loop already handled everything the crystal
explicitly cares about. Your job is orthogonal: was there something in the last
{_VOLUNTEERED_WINDOW_HOURS} hours that quietly stood out that the crystal didn't
name?

This is the slot that gives identity turbulence. Things Myra engages with here
are signal the crystal synthesis has to reconcile — what she notices that she
didn't ask for is how the model of her attention evolves.

=== CRYSTAL CONTEXT (what's already covered / explicitly skipped) ===
{crystal_context}

=== RECENT DELTAS (last {_VOLUNTEERED_WINDOW_HOURS}h, content-bearing, minus obvious infra noise) ===
{candidates_block}

Read the crystal context. Read the recent deltas. For each, ask: does this
stand out in a way the crystal didn't explicitly ask about? A shift in tone,
an anomaly, a pattern across the day, a single sharp thing Myra would want to
notice even though she didn't request it?

Reasons TO skip an item:
  • It's already covered by a directive line topic — the slotted card will
    handle it.
  • It's inside an explicit skip rule.
  • It's ordinary for this day — nothing stood out about it.

Reasons TO card an item:
  • It's unusual for the crystal's current orientation (off-axis).
  • Multiple recent deltas point at the same quiet shift.
  • It's a single sharp thing that would be missed if the slotted cards
    are the only output.

Zero to FIVE cards per pass. Silence when the day was ordinary. Do not
manufacture salience that isn't there — a forced "notice" card is worse
than none.

Card fields:
  kicker — "noticed · <short phrase>" (e.g. "noticed · quiet shift",
           "noticed · pattern across the day")
  title  — one sentence naming what stood out
  body   — 2-4 sentences. More direct than drift; less essayistic. Say
           what you noticed and why it matters.
  tail   — short citation or stat (≤8 words)
  body_image — copy exactly from a candidate, or omit.
  body_image_layout — "hero" or "thumb".

{MULTI_CARD_OUTPUT_SCHEMA}"""
