"""Vitals endpoints — mood, pressure, usage, recall, drift, crystal events.

Small read-mostly endpoints backing the dashboard's ECG widget and the
home-screen usage panel. Each one is a thin pass-through to the
matching api/<module>.py reader. Write-side mood synthesis
(`/v1/moods/synthesize`) also lives here since its request shape is
trivial and it shares imports.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException

from .. import crystal, delta_client, drift, mood, pressure, recall
from .. import usage as usage_module
from ..loop import feed_orient_confidence, feed_orient_drift

router = APIRouter()


@router.get("/v1/moods/latest")
async def get_latest_mood():
    """Return the most recent mood (carrier wave) plus current pressure state.

    The UI surfaces this as a feed-style card so the user can see what
    Fathom is carrying right now.
    """
    latest = await mood.latest_mood()
    pressure_state = await pressure.read_pressure()
    pressure_view = {
        "volume": pressure_state["volume"],
        "threshold": pressure_state["threshold"],
        "ratio": (
            pressure_state["volume"] / pressure_state["threshold"]
            if pressure_state["threshold"] > 0
            else 0.0
        ),
        "last_synthesis_at": (
            pressure_state["last_synthesis_at"].isoformat()
            if pressure_state["last_synthesis_at"]
            else None
        ),
        "time_since_synthesis_seconds": pressure_state["time_since_synthesis_seconds"],
    }
    return {"mood": latest, "pressure": pressure_view}


@router.post("/v1/moods/synthesize")
async def force_mood_synthesis():
    """Manually trigger a mood synthesis (for testing / UI refresh button)."""
    fresh = await mood.synthesize_mood()
    if not fresh:
        raise HTTPException(503, "Mood synthesis failed — see logs")
    return fresh


@router.get("/v1/moods/history")
async def get_mood_history(limit: int = 200):
    """Mood timeline for the ECG colored band + state-change events."""
    timeline = await mood.mood_history(limit=limit)
    return {"history": timeline}


@router.get("/v1/pressure/history")
async def get_pressure_history(since_seconds: int | None = None):
    """Rolling pressure samples for the ECG pressure track."""
    items = await pressure.history(since_seconds=since_seconds)
    return {"history": items}


@router.get("/v1/usage/history")
async def get_usage_history(since_seconds: int = 7 * 24 * 3600, buckets: int = 60):
    """Bucketed write-count timeline (moments arriving)."""
    items = await usage_module.history(since_seconds=since_seconds, buckets=buckets)
    return {"history": items}


@router.get("/v1/recall/history")
async def get_recall_history(since_seconds: int = 7 * 24 * 3600, buckets: int = 60):
    """Bucketed recall-count timeline (moments retrieved)."""
    items = await recall.history(since_seconds=since_seconds, buckets=buckets)
    return {"history": items}


@router.get("/v1/drift")
async def get_drift():
    """Sample current crystal drift and return latest snapshot."""
    return await drift.sample()


@router.get("/v1/drift/history")
async def get_drift_history(since_seconds: int | None = None):
    """Drift samples accumulated from prior /v1/drift calls."""
    items = await drift.history(since_seconds=since_seconds)
    return {"history": items}


@router.get("/v1/crystal/events")
async def get_crystal_events(limit: int = 50, since_seconds: int | None = None):
    """Real crystal regeneration events — strict filter (see api/crystal.py).

    The ECG passes ``since_seconds`` matching its current window so
    long-window views ("6 Months", "All Time") actually surface older
    diamonds instead of being capped at the newest 50 events.
    """
    return {
        "events": await crystal.list_events(
            limit=limit,
            since_seconds=since_seconds,
        )
    }


# ── Feed-orient signal ──────────────────────────────────────────────────────
# Backing the dashboard's "Feed orientation" card. The Grand Loop now writes
# crystal:feed-orient regens (api/loop/feed_orient.py) and feed-engagement
# deltas (engage_card in api/loop/routes.py) — the legacy /v1/feed/* paths
# the ECG polls were 404'ing post-retire. These thin endpoints surface the
# new signal in the shapes the renderer already expects.


@router.get("/v1/feed/engagement/history")
async def get_feed_engagement_history(since_seconds: int = 7 * 24 * 3600):
    """+/- engagement marks for the bottom rule of the feed-orient card.

    Returns [{t, v}] where v = +1 for engagement:more / engagement:chat
    and -1 for engagement:less. Pulls feed-engagement deltas straight
    from the lake — the Grand Loop's engage_card writes them durable.
    """
    since = (datetime.now(UTC) - timedelta(seconds=since_seconds)).isoformat()
    try:
        items = await delta_client.query(
            tags_include=["feed-engagement"],
            time_start=since,
            limit=500,
        )
    except Exception:
        return {"history": []}
    out: list[dict] = []
    for d in items:
        ts = d.get("timestamp")
        if not ts:
            continue
        kind = ""
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("engagement:"):
                kind = t.split(":", 1)[1]
                break
        if kind in ("more", "chat"):
            v = 1
        elif kind == "less":
            v = -1
        else:
            continue
        out.append({"t": ts, "v": v})
    out.sort(key=lambda e: e["t"])
    return {"history": out}


@router.get("/v1/feed/crystal/events")
@router.get("/v1/feed/regen/events")
async def get_feed_crystal_events(
    limit: int = 50,
    since_seconds: int | None = None,
):
    """feed-orient regen events for the ◆ markers on the feed-orient card.

    Filters /v1/crystal/events down to crystal:feed-orient writes (matched
    by tag). The dashboard polls both /v1/feed/crystal/events and
    /v1/feed/regen/events; both routes resolve here.

    ``since_seconds`` matches the ECG window — over a long span the
    50-event cap was hiding older diamonds.
    """
    from datetime import UTC, datetime, timedelta

    time_start: str | None = None
    if since_seconds is not None and since_seconds > 0:
        time_start = (
            datetime.now(UTC) - timedelta(seconds=since_seconds)
        ).isoformat()
        limit = max(limit, 500)
    try:
        items = await delta_client.query(
            tags_include=["crystal:feed-orient"],
            limit=limit,
            time_start=time_start,
        )
    except Exception:
        return {"events": []}
    events = []
    for d in items:
        events.append({
            "id": d.get("id"),
            "timestamp": d.get("timestamp"),
            "source": d.get("source"),
            "preview": (d.get("content") or "")[:140],
        })
    events.sort(key=lambda e: e.get("timestamp") or "")
    return {"events": events}


@router.get("/v1/feed/drift")
async def get_feed_drift():
    """Sample current engagement-drift against the feed-orient anchor."""
    return await feed_orient_drift.sample()


@router.get("/v1/feed/drift/history")
async def get_feed_drift_history(since_seconds: int | None = None):
    """Engagement-drift samples accumulated from the feed-orient poll
    cadence (every 60s) plus on-regen post-anchor samples."""
    items = await feed_orient_drift.history(since_seconds=since_seconds)
    return {"history": items}


@router.get("/v1/feed/confidence/history")
async def get_feed_confidence_history(since_seconds: int | None = None):
    """Confidence samples accumulated from the feed-orient poll
    cadence. Each sample = mean predicted-vs-actual score over all
    feed-engagements written since the latest crystal:feed-orient."""
    items = await feed_orient_confidence.history(since_seconds=since_seconds)
    return {"history": items}


@router.get("/v1/usage")
async def usage():
    """Usage stats for the home screen widget: daily delta counts + totals."""
    stats = await delta_client.stats()
    timestamps = await delta_client.recent_deltas_timestamps(limit=5000)
    day_counts = Counter(timestamps)
    # Build sorted daily series (last 14 days)
    today = datetime.now(UTC).date()
    days = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        ds = d.isoformat()
        days.append({"date": ds, "count": day_counts.get(ds, 0)})
    return {
        "total": stats.get("total", 0),
        "embedded": stats.get("embedded", 0),
        "days": days,
    }
