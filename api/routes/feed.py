"""Feed endpoints — orient crystal, drift, engagement, stories.

Twelve routes backing the feed loop:
- Feed loop control: /v1/feed/refresh, /v1/feed/visit, /v1/feed/status
- Feed-orient crystal: /v1/feed/crystal[/refresh], /v1/feed/crystal/events
- Drift tracking: /v1/feed/drift, /v1/feed/drift/history
- Confidence + engagement history for the ECG card
- Stories + engagement: /v1/feed/stories, /v1/feed/engagement

Thin HTTP wrapper around api/feed_loop.py and api/feed_crystal.py.
Every contact-scoped endpoint reads caller slug via auth.current_contact_slug.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import auth, delta_client, feed_crystal, feed_loop

router = APIRouter()


class FeedEngagementRequest(BaseModel):
    # Six kinds today:
    #   "more"        → +1 explicit interest (the + button)
    #   "less"        → -1 explicit disinterest (the − button)
    #   "chat"        → +1 (a message in a chat session opened from a card)
    #   "dismiss"     → -1 explicit dismissal (× / hide)
    #   "scroll-past" → soft -0.5 (rendered, scrolled past without engagement)
    #   "dwell-low"   → soft -0.5 (rendered, viewer left fast)
    kind: str
    card_id: str
    topic: str | None = None
    card_excerpt: str | None = None
    chat_session: str | None = None


# Single source of truth for the kinds the engagement endpoint accepts +
# the sign each one carries when it later feeds the confidence scorer.
# Keep aligned with feed_crystal._engagement_sign.
ENGAGEMENT_KINDS: dict[str, float] = {
    "more": 1.0,
    "less": -1.0,
    "chat": 1.0,
    "dismiss": -1.0,
    "scroll-past": -0.5,
    "dwell-low": -0.5,
}


@router.post("/v1/feed/refresh")
async def refresh_feed(request: Request):
    """Manual kick of the feed loop, bypassing the visit-debounce.

    Still respects the per-contact single-flight lock — repeated calls
    during a fire return `fired=False, reason=already-running`. Useful
    for debugging and for any external trigger that wants to force a
    regen.
    """
    slug = auth.current_contact_slug(request)
    return await feed_loop.force_fire(slug, reason="manual-refresh")


@router.post("/v1/feed/visit")
async def feed_visit(request: Request):
    """Page-view ping. Schedules a debounced fire (cooldown in settings)."""
    slug = auth.current_contact_slug(request)
    return await feed_loop.mark_visit(slug)


@router.get("/v1/feed/status")
async def feed_status(request: Request):
    """Current loop state for the UI's "generating…" indicator."""
    slug = auth.current_contact_slug(request)
    return feed_loop.current_status(slug)


@router.get("/v1/feed/regen/events")
async def feed_regen_events(request: Request, limit: int = 100):
    """Feed-loop regen history for the Weather Stats graph.

    Each successful (or attempted) `_run_once` writes a `feed-regen-event`
    delta tagged with the contact slug. This endpoint surfaces them as
    timestamps the chart can mark.
    """
    slug = auth.current_contact_slug(request)
    try:
        results = await delta_client.query(
            tags_include=["feed-regen-event", f"contact:{slug}"],
            limit=limit,
        )
    except Exception:
        return {"events": []}
    events = []
    for d in results:
        body = d.get("content") or "{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        events.append({
            "id": d.get("id"),
            "timestamp": d.get("timestamp"),
            "reason": payload.get("reason"),
            "outcome": payload.get("outcome"),
        })
    events.sort(key=lambda e: e.get("timestamp") or "")
    return {"events": events[-limit:]}


@router.get("/v1/feed/crystal")
async def get_feed_crystal(request: Request):
    """Latest crystal:feed-orient delta for the current contact."""
    slug = auth.current_contact_slug(request)
    c = await feed_crystal.latest(slug, force=True)
    if not c:
        return {"crystal": None}
    return {
        "crystal": {
            "id": c.get("id"),
            "created_at": c.get("created_at"),
            "confidence": c.get("confidence"),
            "narrative": c.get("narrative"),
            "directive_lines": c.get("directive_lines"),
            "topic_weights": c.get("topic_weights"),
            "skip_rules": c.get("skip_rules"),
        }
    }


@router.post("/v1/feed/crystal/refresh")
async def refresh_feed_crystal(request: Request):
    """Manually run a feed-orient crystal regeneration (no wake-gate check)."""
    slug = auth.current_contact_slug(request)
    fresh = await feed_crystal.synthesize(slug)
    if not fresh:
        raise HTTPException(500, "synthesis failed — check server logs")
    return {"status": "ok", "id": fresh.get("id"), "confidence": fresh.get("confidence")}


@router.get("/v1/feed/crystal/events")
async def feed_crystal_events(request: Request, limit: int = 50):
    """Crystal regeneration history (for the ECG card)."""
    slug = auth.current_contact_slug(request)
    events = await feed_crystal.list_events(slug, limit=limit)
    return {"events": events}


@router.get("/v1/feed/drift")
async def feed_drift(request: Request):
    """Sample current engagement-centroid drift now."""
    slug = auth.current_contact_slug(request)
    return await feed_crystal.sample_drift(slug)


@router.get("/v1/feed/drift/history")
async def feed_drift_history(request: Request, since_seconds: int | None = None):
    """Drift history for the ECG card."""
    slug = auth.current_contact_slug(request)
    return {"history": feed_crystal.drift_history(slug, since_seconds=since_seconds)}


@router.get("/v1/feed/confidence/history")
async def feed_confidence_history(request: Request, limit: int = 50):
    """Confidence over time, derived from the confidence: tag on each crystal regen."""
    slug = auth.current_contact_slug(request)
    events = await feed_crystal.list_events(slug, limit=limit)
    return {
        "history": [
            {"t": e.get("timestamp"), "v": e.get("confidence")}
            for e in events
            if e.get("confidence") is not None
        ]
    }


@router.get("/v1/feed/engagement/history")
async def feed_engagement_history(
    request: Request,
    since_seconds: int = 7 * 24 * 3600,
    limit: int = 500,
):
    """Engagement marks for the ECG bottom rule. Returns time + sign per delta."""
    slug = auth.current_contact_slug(request)
    cutoff = (datetime.now(UTC) - timedelta(seconds=since_seconds)).isoformat()
    try:
        deltas = await delta_client.query(
            tags_include=["feed-engagement", f"contact:{slug}"],
            time_start=cutoff,
            limit=limit,
        )
    except Exception:
        deltas = []
    out = []
    for d in deltas:
        kind = ""
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("engagement:"):
                kind = t.split(":", 1)[1]
                break
        if not kind:
            continue
        sign = ENGAGEMENT_KINDS.get(kind, 0.0)
        if not sign:
            continue
        out.append({"t": d.get("timestamp"), "v": sign, "k": kind})
    return {"history": out}


@router.get("/v1/feed/levels")
async def feed_levels(request: Request, since_seconds: int = 7 * 24 * 3600):
    """Counts of feed cards by level over the last N seconds.

    Used by the UI verbosity dropdown to show "ALERT (3) · NOTICE (12) · …"
    so the user can see at a glance how much is hidden behind the current
    filter setting. The dropdown's default lands on `feed_default_visible_level`
    from settings.
    """
    from .._feed_router import LEVEL_ORDER
    from ..settings import settings as _s

    slug = auth.current_contact_slug(request)
    cutoff = (datetime.now(UTC) - timedelta(seconds=since_seconds)).isoformat()
    try:
        deltas = await delta_client.query(
            tags_include=["feed-card", f"contact:{slug}"],
            time_start=cutoff,
            limit=500,
        )
    except Exception:
        deltas = []

    counts: dict[str, int] = {lvl: 0 for lvl in LEVEL_ORDER}
    untagged = 0
    for d in deltas:
        level = ""
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("level:"):
                level = t.split(":", 1)[1]
                break
        if level in counts:
            counts[level] += 1
        else:
            # Pre-rebuild cards that don't carry a level: tag yet.
            # Treat them as NOTICE so existing feeds keep rendering on
            # the default filter — backfilling is a separate concern.
            untagged += 1

    return {
        "default_level": _s.feed_default_visible_level,
        "order": list(LEVEL_ORDER),
        "counts": counts,
        "untagged": untagged,
    }


@router.get("/v1/feed/stories")
async def get_feed_stories(request: Request, limit: int = 20, offset: int = 0):
    """Proxy to delta-store's feed stories endpoint, scoped to current contact."""
    slug = auth.current_contact_slug(request)
    return await delta_client.feed_stories(limit=limit, offset=offset, contact_slug=slug)


@router.post("/v1/feed/engagement")
async def write_feed_engagement(req: FeedEngagementRequest, request: Request):
    """Capture a feed engagement signal — input to the feed-orient crystal.

    Only three kinds count: `more` (the + button), `less` (the − button),
    and `chat` (a message in a chat session opened from a card). Click
    alone is not engagement; the chat session it opens is. See
    docs/feed-spec.md.
    """
    kind = (req.kind or "").lower()
    if kind not in ENGAGEMENT_KINDS:
        raise HTTPException(400, f"unknown engagement kind: {kind!r}")
    if not req.card_id:
        raise HTTPException(400, "card_id required")

    contact = getattr(request.state, "contact", None)
    contact_slug = (contact or {}).get("slug")

    # Note on tagging: deliberately NOT using `chat:<slug>` for the chat
    # session linkage — that tag belongs to the chat-listener's session
    # roster, and an engagement delta tagged with it would be processed
    # as a user message and trip an inference turn on the JSON payload.
    # Use `chat-from:<slug>` instead — same retrieval ergonomics, no
    # collision with the listener's chat-trigger filter.
    tags = ["feed-engagement", f"engagement:{kind}", f"engages:{req.card_id}"]
    if req.topic:
        tags.append(f"topic:{req.topic}")
    if req.chat_session:
        tags.append(f"chat-from:{req.chat_session}")
    if contact_slug:
        tags.append(f"contact:{contact_slug}")

    payload = {
        "kind": kind,
        "card_id": req.card_id,
        "topic": req.topic or "",
        "card_excerpt": (req.card_excerpt or "")[:200],
    }
    if req.chat_session:
        payload["chat_session"] = req.chat_session

    written = await delta_client.write(
        content=json.dumps(payload, ensure_ascii=False),
        tags=tags,
        source="consumer-api",
    )
    return {"status": "ok", "id": written.get("id")}
