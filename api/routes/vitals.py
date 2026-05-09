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

    The UI surfaces this as a feed-style card so the user can see what
    Fathom is carrying right now. When the most recent mood-regen
    crashed (LLM provider error), the prior mood stays in place and a
    `regen_error` envelope is attached so the UI can render a stale
    indicator.
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
    regen_error = await _latest_mood_regen_error(latest)
    return {"mood": latest, "pressure": pressure_view, "regen_error": regen_error}


async def _latest_mood_regen_error(latest_mood: dict | None) -> dict | None:
    """Return the most recent mood-regen-error if it's newer than the
    latest successful mood — meaning the operator should see "(stale)".
    Returns None when the most recent regen succeeded (or no errors yet).
    """
    try:
        rows = await delta_client.query(
            tags_include=["mood-regen-error"],
            limit=1,
        )
    except Exception:
        return None
    if not rows:
        return None
    err = rows[0]
    err_ts = err.get("timestamp") or ""
    mood_ts = (latest_mood or {}).get("timestamp") or ""
    if mood_ts and err_ts <= mood_ts:
        return None
    import json as _json

    try:
        payload = _json.loads(err.get("content") or "{}")
    except Exception:
        payload = {}
    return {
        "timestamp": err_ts,
        "role": payload.get("role") or "Mood synthesis",
        "class": payload.get("class") or "",
        "message": payload.get("message") or "",
    }


@router.post("/v1/moods/synthesize")
async def force_mood_synthesis():
    """Manually trigger a mood synthesis (for testing / UI refresh button)."""
    fresh = await mood.synthesize_mood()
    if not fresh:
        raise HTTPException(503, "Mood synthesis failed — see logs")
    return fresh


@router.get("/v1/llm/status")
async def get_llm_status():
    """Return whether the LLM is currently failing.

    Compares the most recent error envelope across known LLM-error tag
    families (`system-error` from the harness, `mood-regen-error` from
    mood synth) against the most recent successful LLM-using producer
    write (`mood-delta`, `kind:harness-turn`, `kind:sediment`). If a
    success is newer than the latest error, the LLM is back; otherwise
    the error is still live.

    Replaces the inline mood-stale indicator with a top-of-page banner
    that auto-clears as soon as any LLM-using process succeeds — so the
    moment the user adds tokens and the next mood/harness fire lands,
    the warning disappears on its own.
    """
    err_ts = ""
    err_payload: dict = {}
    for tag in ("system-error", "mood-regen-error"):
        try:
            rows = await delta_client.query(tags_include=[tag], limit=1)
        except Exception:
            continue
        if not rows:
            continue
        row = rows[0]
        ts = row.get("timestamp") or ""
        if ts > err_ts:
            err_ts = ts
            import json as _json

            try:
                err_payload = _json.loads(row.get("content") or "{}")
            except Exception:
                err_payload = {"message": row.get("content") or ""}

    if not err_ts:
        return {"ok": True, "error": None}

    # Any LLM-using producer success newer than the error clears it.
    success_ts = ""
    for tag in ("mood-delta", "kind:harness-turn", "kind:sediment"):
        try:
            rows = await delta_client.query(
                tags_include=[tag],
                time_start=err_ts,
                limit=1,
            )
        except Exception:
            continue
        if not rows:
            continue
        ts = rows[0].get("timestamp") or ""
        if ts > success_ts:
            success_ts = ts
    if success_ts and success_ts > err_ts:
        return {"ok": True, "error": None}

    return {
        "ok": False,
        "error": {
            "timestamp": err_ts,
            "role": err_payload.get("role") or "LLM",
            "class": err_payload.get("class") or "",
            "message": err_payload.get("message") or "",
        },
    }


@router.get("/v1/moods/history")
async def get_mood_history(limit: int = 200):
    """Mood timeline for the ECG colored band + state-change events."""
    timeline = await mood.mood_history(limit=limit)
    return {"history": timeline}


@router.get("/v1/weather/events")
async def get_weather_events(since_seconds: int = 7 * 24 * 3600):
    """Mood-synthesis + relief-tier fire events, ready for the Weather
    chart's event-marker layer.

    Each entry: `{kind, timestamp, reason?, state?}` where `kind` is
    one of: `mood`, `alert`, `bridging`, `reflection`, `drift`.

    Sources:
      · `mood`     — `fathom-mood` carrier-wave writes
      · `alert/…`  — thread messages the relief watcher (or manual
                     button) emits with `pressure-tier:<name>`
                     and `pressure-reason:<reason>` tags

    Both auto fires and manual button fires are returned; the `reason`
    field distinguishes ("pressure" / "contrast-wake" for auto,
    "manual-button-…" for clicks).
    """
    from datetime import UTC, datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(seconds=since_seconds)).isoformat()

    events: list[dict] = []

    # Mood-synthesis carrier-wave events
    try:
        moods = await delta_client.query(
            tags_include=["mood-delta"],
            source="fathom-mood",
            time_start=cutoff,
            limit=500,
        )
        for d in moods:
            tags = d.get("tags") or []
            state = ""
            for t in tags:
                if isinstance(t, str) and t.startswith("feeling:"):
                    state = t.split(":", 1)[1]
                    break
            events.append(
                {
                    "kind": "mood",
                    "timestamp": d.get("timestamp"),
                    "state": state,
                }
            )
    except Exception:
        pass

    # Relief-tier fires — one query per tier, since the lake's tag
    # filter is AND across tags_include and we want exactly one tier
    # per query for clean kind labeling.
    for tier_name in ("alert", "bridging", "reflection", "drift"):
        try:
            rows = await delta_client.query(
                tags_include=[f"pressure-tier:{tier_name}"],
                time_start=cutoff,
                limit=500,
            )
            for d in rows:
                tags = d.get("tags") or []
                reason = ""
                for t in tags:
                    if isinstance(t, str) and t.startswith("pressure-reason:"):
                        reason = t.split(":", 1)[1]
                        break
                events.append(
                    {
                        "kind": tier_name,
                        "timestamp": d.get("timestamp"),
                        "reason": reason,
                    }
                )
        except Exception:
            pass

    events.sort(key=lambda e: e.get("timestamp") or "")
    return {"events": events}


@router.get("/v1/mood/topology")
async def get_mood_topology():
    """Per-axis affect topology — the star map.

    Each axis row carries:
      · `level` — current intensity (0–1) named by the carrier-wave
      · `net` — signed drift since the carrier-wave, summed from
        accumulated `kind:mood-shift` deltas
      · `shifts` / `reasons` — count + sample reasons backing the drift

    The synthesizer reads prior levels + drift and integrates a new
    levels map at each carrier-wave; the chart renders levels as
    spoke length and drift as directional decoration. Open vocabulary
    on the synthesizer side, with synonyms collapsed at read time
    (focus/focused/focusing → focus).

    Read-only on purpose — mood is Fathom's self-report, not a survey
    instrument. Powers the mood-star widget on the dashboard.
    """
    return await mood.compute_topology()


@router.get("/v1/pressure/history")
async def get_pressure_history(since_seconds: int | None = None):
    """Rolling pressure samples for the ECG pressure track."""
    items = await pressure.history(since_seconds=since_seconds)
    return {"history": items}


@router.get("/v1/feed-pressure/history")
async def get_feed_pressure_history(since_seconds: int | None = None):
    """Rolling FEED-pressure samples — the curve relief tiers operate on.

    Sibling of `/v1/pressure/history` (which serves mood-pressure).
    Same primitive, different source weights — see `api/feed_pressure.py`.
    Used by the dashboard's pressure chart so the relief tier floor
    lines (alert / bridging / sit) land on the curve they actually gate.
    """
    from .. import feed_pressure

    items = await feed_pressure.history(since_seconds=since_seconds)
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


@router.post("/v1/feed/regen/fire")
async def fire_feed_regen():
    """Manual feed-orient regen.

    Bypasses the engagement-count threshold (a click means the user
    wants this regardless of substrate signal) but still respects the
    30-minute cooldown so spamming can't loop-pin the regen.

    Powered by the same `_run_regen` path as the auto-fire watcher;
    cooldown stamping happens implicitly via the new `crystal:feed-orient`
    delta's timestamp.
    """
    from ..loop import feed_orient

    result = await feed_orient.force_run_regen()
    return {"ok": True, **result}


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
        time_start = (datetime.now(UTC) - timedelta(seconds=since_seconds)).isoformat()
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
        events.append(
            {
                "id": d.get("id"),
                "timestamp": d.get("timestamp"),
                "source": d.get("source"),
                "preview": (d.get("content") or "")[:140],
            }
        )
    events.sort(key=lambda e: e.get("timestamp") or "")
    return {"events": events}


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
