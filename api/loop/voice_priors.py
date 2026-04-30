"""Voice priors — accumulating standing from past parliaments.

Phase 4a of the River refactor. Voices that contributed to fires the
judge rated well earn `kind:voice-affirmation` deltas in the lake;
this module reads them back as a per-voice "standing score" the
convener can use to bias voice selection.

The signal is intentionally simple: count of recent affirmations per
voice, normalized to [0, 1] across the active voice set. Refinements
(time-decay, judge-axis-weighting, voice-specific TTL) layer on later;
for now the count is enough to validate the closed-loop pattern.

  get_voice_priors(window_hours=168) → {voice_name: float in [0,1]}

Reads from lake. Cheap (one tag-filtered query, capped at 200 deltas).
Soft-fails to empty dict on lake error — convener falls back to a
no-priors prompt without breaking.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from .. import delta_client

log = logging.getLogger(__name__)

# How far back to look for affirmations. A week catches a meaningful
# arc of fires without letting stale-pattern voices dominate the
# convener's prompt indefinitely. Tunable as we learn what "recent"
# means for the loop's actual cadence.
_DEFAULT_WINDOW_HOURS = 168

# Cap the per-query lake fetch. Sufficient for a week of typical
# fire density; scales linearly with how many voices fire per day.
_MAX_AFFIRMATIONS_FETCHED = 200


async def get_voice_priors(
    window_hours: int = _DEFAULT_WINDOW_HOURS,
) -> dict[str, float]:
    """Per-voice standing score from recent affirmations.

    Returns a mapping from voice name to float in [0, 1], where 1.0
    means "this voice has the most recent affirmations of any voice
    in the window" and 0.0 means "no affirmations." Empty dict when
    the lake has no affirmations yet (cold-start) or when the query
    fails. Voices not present in the dict are NOT zero-rated; they're
    simply unsignaled — the convener should treat their absence as
    "no signal," not "explicitly low standing."
    """
    since = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
    try:
        rows = await delta_client.query(
            tags_include=["kind:voice-affirmation"],
            time_start=since,
            limit=_MAX_AFFIRMATIONS_FETCHED,
        )
    except Exception:
        log.exception("voice_priors: lake query failed")
        return {}
    if not rows:
        return {}

    # Tally affirmations per voice. The voice name comes from the
    # `voice:<name>` tag the witness wrote on each affirmation.
    counts: Counter[str] = Counter()
    for d in rows:
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("voice:"):
                voice_name = t.split(":", 1)[1].strip()
                if voice_name:
                    counts[voice_name] += 1
                break

    if not counts:
        return {}
    max_count = max(counts.values())
    if max_count <= 0:
        return {}

    # Normalize so the most-affirmed voice scores 1.0 and others
    # scale relative to it. Keeps the score interpretable across
    # different fire densities — a quiet week and a busy week both
    # produce 0..1 scores, just with different absolute counts behind
    # them.
    return {voice: count / max_count for voice, count in counts.items()}


def render_priors_for_prompt(priors: dict[str, float]) -> str:
    """Compact prose for the convener prompt.

    Voices listed in descending order of standing. Empty string when
    no priors exist — convener handles that as "no recent signal,
    pick voices fresh." Below 0.2 we skip the entry (noise floor)
    so the prompt isn't bloated with one-off voices that fired once
    six days ago.
    """
    if not priors:
        return ""
    items = sorted(priors.items(), key=lambda kv: -kv[1])
    lines: list[str] = []
    for name, score in items:
        if score < 0.2:
            continue
        lines.append(f"  · {name} (standing {score:.2f})")
    if not lines:
        return ""
    return "\n".join(lines)
