"""Feed discrepancy pass — internal contradiction detection.

This pass looks for places where the user's recent statements diverge
from their earlier statements. Not because they were wrong before — but
because their thinking has *moved*, and naming the divergence might be
worth seeing.

This is the uncomfortable-truth lane. It exists specifically to prevent
flattery drift: a memory system that only ever shows you what you want
to see slowly stops being able to disagree with you. Discrepancy is
the structural counterweight.

Capped at 1 card per fire. Surfacing more than one discomfort per cycle
is piling on. A drifted opinion is normal; a genuinely contradicted
commitment is the rare case worth surfacing — don't reach.

Cards from this pass are tagged with low comfort + high salience by the
judge, which routes them to ALERT (the uncomfortable-truth gate) when
the contradiction is sharp.

Pure read/format logic. The fire entrypoint + write lives in
feed_loop.py alongside the per-line and drift paths.
"""

from __future__ import annotations

from datetime import timedelta

from . import delta_client
from ._time import now as _now

# Recent window — what positions are *currently* being held.
_RECENT_DAYS = 7
# Older window — the prior positions to compare against.
_OLDER_MIN_DAYS = 30

_EXCLUDE_TAGS = [
    "chat-event",
    "feed-card",
    "feed-story",
    "feed-engagement",
    "agent-heartbeat",
    "silence",
    "mood-delta",
]


async def fetch_recent_user_positions(window_days: int = _RECENT_DAYS) -> list[dict]:
    """Recent user-authored content. Tagged participant:user are chat
    turns; we also include claude-code work (the user's own typing) since
    code-side decisions and chat-side opinions form one consistent
    voice."""
    cutoff = (_now() - timedelta(days=window_days)).isoformat()
    try:
        results = await delta_client.query(
            tags_include=["participant:user"],
            tags_exclude=_EXCLUDE_TAGS,
            time_start=cutoff,
            limit=120,
        )
    except Exception:
        results = []
    out: list[dict] = []
    for d in results:
        content = (d.get("content") or "").strip()
        if len(content) < 60:
            continue
        out.append(d)
    return out


async def fetch_older_user_positions(min_days: int = _OLDER_MIN_DAYS) -> list[dict]:
    """Older user-authored content. The candidate pool the recent
    statements get compared against."""
    cutoff = (_now() - timedelta(days=min_days)).isoformat()
    try:
        results = await delta_client.query(
            tags_include=["participant:user"],
            tags_exclude=_EXCLUDE_TAGS,
            time_end=cutoff,
            limit=120,
        )
    except Exception:
        results = []
    out: list[dict] = []
    for d in results:
        content = (d.get("content") or "").strip()
        if len(content) < 80:
            continue
        out.append(d)
    return out


def format_user_positions(deltas: list[dict], header: str) -> str:
    """Compact format for either window."""
    if not deltas:
        return f"{header}\n  (no qualifying entries)"
    lines: list[str] = [header]
    for d in deltas:
        ts = (d.get("timestamp") or "")[:10]
        did = (d.get("id") or "")[:12]
        content = (d.get("content") or "").strip().replace("\n", " ")[:260]
        lines.append(f"  [{ts}] ({did}) {content}")
    return "\n".join(lines)


def build_discrepancy_directive(recent_text: str, older_text: str) -> str:
    return f"""\
You are running the DISCREPANCY pass on Fathom. Your job is to notice \
when the user's recent statements diverge from their earlier statements \
— not because they were wrong, but because their thinking has *moved* \
and the divergence might be worth seeing.

This is the uncomfortable-truth lane. The system needs it specifically \
to prevent flattery drift: a memory that only ever shows what the user \
wants to see slowly stops being able to disagree with them.

=== RECENT POSITIONS (last {_RECENT_DAYS}d) ===
{recent_text}

=== OLDER POSITIONS (>{_OLDER_MIN_DAYS}d ago) ===
{older_text}

Look across the two windows. Find a place where today's stated position \
contradicts an older stated position. Not "thought has refined" — \
actual contradiction, where the older statement and the newer statement \
cannot both be true.

Good discrepancies:
  • Older: "we'll never use postgres for the lake." Recent: "the lake \
is postgres-backed."
  • Older: "this approach is a dead end." Recent: actively implementing \
that approach.
  • A stated principle that today's behavior contradicts.

Surface gently — this is not a gotcha pass. The framing is "here's a \
place your thinking has moved; want to look?" not "you were wrong then" \
or "you're wrong now."

If no real divergence exists in the windows, return `{{"cards": []}}`. \
That's the normal outcome. A drifted opinion is healthy human thought; \
a genuinely contradicted commitment is the rare case. Don't reach.

Cap at 1 card. Only the sharpest contradiction.

Card schema (the cards array, with at most one entry):
  kicker — "Discrepancy"
  title  — the divergence, named neutrally (≤120 chars)
  body   — 2-3 sentences. What was said before, what's happening now, \
*no judgment*. Just the shape of the change.
  tail   — ≤8 words. Pointer to the older delta (date + brief tag).
  body_image — empty
  link   — empty

Respond with ONLY a JSON object {{"cards": [...]}}. No markdown fences."""
