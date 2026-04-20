"""Usage timeline — sibling to pressure, but unweighted and undecayed.

Pressure asks "how much salient activity has built up since I last
checked in" (weighted by source, exponentially decayed, reset at every
mood synthesis).

Usage asks "how busy was the lake at each moment" — a raw count of
fragments-per-bucket across the window. No weights, no decay, no reset.

Bucketing happens in the delta-store via SQL GROUP BY, so the timeline
is not subject to any row-limit truncation — every delta in the window
is counted, not just the most recent N.
"""

from __future__ import annotations

from . import delta_client


async def history(since_seconds: int, buckets: int = 60) -> list[dict]:
    """Return delta-count timeline across the window in N buckets.

    Each entry: {t: iso, v: int}. v is a raw count, not weighted.
    """
    if since_seconds <= 0 or buckets <= 0:
        return []
    try:
        return await delta_client.usage_history(
            since_seconds=since_seconds, buckets=buckets
        )
    except Exception:
        return []
