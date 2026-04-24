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

router = APIRouter()


@router.get("/v1/moods/latest")
async def get_latest_mood():
    """Return the most recent mood (carrier wave) plus current pressure state.

    The UI surfaces this as a feed-style card so Myra can see what Fathom
    is carrying right now.
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
async def get_crystal_events(limit: int = 50):
    """Real crystal regeneration events — strict filter (see api/crystal.py)."""
    return {"events": await crystal.list_events(limit=limit)}


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
