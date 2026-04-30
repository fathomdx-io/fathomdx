"""Judge history aggregation per intent kind.

Phase 5b of the River refactor. The convener used to pick depth purely
from intent body + recall + standpoint + voice priors. This module
adds one more signal: how have past fires of THE SAME intent kind
gone? If the last 5 reflection pulses averaged 0.4 confidence and
never settled below 0.18 spread, the convener should weigh that
when picking depth this time — maybe minimal worked better, maybe
full just hammered against unreachable convergence.

  recent_judge_stats_by_kind(kinds) → {kind: {samples, avg_*, ...}}

The witness card stores its judge axes inside the JSON payload's
`axes` field. We pull recent witness lake deltas, parse the JSON,
aggregate per intent kind. Soft-fails to empty dict on lake error.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from .. import delta_client

log = logging.getLogger(__name__)

# How far back to look. A week catches enough fires of any common
# intent kind to be representative without dragging in stale shapes
# from before recent refactors.
_LOOKBACK_HOURS = 7 * 24

# Cap per kind. A handful of recent fires is the signal; older ones
# are noise relative to current cadence.
_PER_KIND_LIMIT = 5

# How many witness cards to fetch in total — enough to fill multiple
# kinds at the per-kind cap with headroom for kinds that are quiet.
_FETCH_LIMIT = 50


async def recent_judge_stats_by_kind(
    kinds: list[str],
) -> dict[str, dict]:
    """Return per-intent-kind average judge axes from recent witness
    cards.

    Output shape: {kind: {samples, avg_salience, avg_resonance,
    avg_confidence}}. Kinds with no recent samples are absent (caller
    should treat absence as "no signal," not "explicitly low").

    Soft-fails to empty dict on lake error.
    """
    if not kinds:
        return {}
    since = (datetime.now(UTC) - timedelta(hours=_LOOKBACK_HOURS)).isoformat()

    # Witness cards are tagged `synthesis` and include `addresses:<id>`
    # for each intent they handled. The intent kind is on the addressed
    # intent itself, not on the card. We fetch the last N witness cards
    # then map each card to its addressed intent's kind via the lake.
    try:
        cards = await delta_client.query(
            tags_include=["synthesis", "addressing-output"],
            time_start=since,
            limit=_FETCH_LIMIT,
        )
    except Exception:
        log.exception("judge_history: card query failed")
        return {}

    # For each card, pull the addresses ids and the intent's kind.
    # Cards carry `addresses:<id>` tags; we'd need to fetch each
    # intent to read its kind. Heavy. Instead, use a lighter heuristic:
    # the kind tag is also stamped on the card itself by the witness
    # output writer (route + kind tags ride along). If that's not
    # present, fall back to an empty kind ("unknown") which matches
    # nothing the caller asked about.
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        kind = ""
        for t in c.get("tags") or []:
            if not isinstance(t, str):
                continue
            # Witness cards inherit the intent kind via the route tag
            # (route:chat-reply / route:feed-card / etc.) and the
            # addressed intent's pulse-kind tags. We look for any tag
            # matching one of the requested kinds.
            for k in kinds:
                if t == k or t == f"kind:{k}" or t == f"pulse:{k}":
                    kind = k
                    break
            if kind:
                break
        if not kind:
            continue

        # Parse axes from card payload JSON.
        try:
            payload = json.loads(c.get("content") or "{}")
            axes = payload.get("axes") or {}
        except (ValueError, TypeError):
            continue

        salience = float(axes.get("salience", 0.0) or 0.0)
        resonance = float(axes.get("resonance", 0.0) or 0.0)
        confidence = float(axes.get("confidence", 0.0) or 0.0)
        by_kind[kind].append(
            {
                "salience": salience,
                "resonance": resonance,
                "confidence": confidence,
            }
        )

    # Aggregate.
    out: dict[str, dict] = {}
    for kind, samples in by_kind.items():
        if not samples:
            continue
        recent = samples[:_PER_KIND_LIMIT]
        n = len(recent)
        out[kind] = {
            "samples": n,
            "avg_salience": sum(s["salience"] for s in recent) / n,
            "avg_resonance": sum(s["resonance"] for s in recent) / n,
            "avg_confidence": sum(s["confidence"] for s in recent) / n,
        }
    return out


def render_judge_history_for_prompt(stats: dict[str, dict]) -> str:
    """Compact prose for the convener prompt. Empty when no kind has
    recent samples (cold-start or quiet week)."""
    if not stats:
        return ""
    lines: list[str] = []
    for kind, agg in sorted(stats.items()):
        n = agg["samples"]
        sal = agg["avg_salience"]
        res = agg["avg_resonance"]
        conf = agg["avg_confidence"]
        lines.append(
            f"  · {kind} (last {n}): salience {sal:.2f}, "
            f"resonance {res:.2f}, confidence {conf:.2f}"
        )
    return "\n".join(lines)
