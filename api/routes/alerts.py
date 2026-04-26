"""Header alerts — derived from unread chat sessions.

An alert is not its own object; it's a *signal* computed from two
sediment streams in the lake:

  • incoming-to-me deltas — anything tagged `fathom-chat` + `contact:<me>`
                            that I didn't write myself
  • my view markers       — `chat-view` deltas tagged `contact:<me>` +
                            `chat:<slug>`

For each session I'm involved in, the latest incoming delta vs. the
latest view marker decides unread state. "Incoming" = anything that
isn't my own typing — Fathom replies, alert-pass DMs, other
participants in shared sessions. The address tag (`for:<me>`) is no
longer required: if a chat message landed in one of my sessions and I
haven't viewed since, the bell rings.

No persistence beyond deltas; no new schema. The view marker is a tiny
per-viewer write that gets reaped on the same lake retention as
everything else.

GET  /v1/alerts          — list unread sessions for the current viewer
POST /v1/alerts          — body {session: slug} marks a session read
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import auth, db, delta_client
from .._tags import tag_suffix

router = APIRouter()

# Bound the alerts query to a recent window — anything older than this
# isn't going to surface as a fresh alert anyway, and pulling 90 days of
# chat-view markers per poll would be wasteful. Matches the session list
# default window so the two stay in sync.
_ALERT_WINDOW_DAYS = 30
_ALERT_QUERY_LIMIT = 1000

# Chat-view deltas are read-receipt sediment — only the latest per
# (viewer, session) is load-bearing, and anything older than the alert
# window can't influence the query anyway. TTL them slightly past the
# window so reaping doesn't race the query. Mark-read fires on every
# session open and on every chat-poll-with-new-messages, so without a
# TTL these would accumulate indefinitely.
_CHAT_VIEW_TTL_DAYS = 35


def _latest_by_slug(deltas: list[dict]) -> dict[str, dict]:
    """For each chat:<slug>, return the most recent delta seen (by timestamp).

    Returns a dict keyed by slug with the full delta as value so callers
    can read both the timestamp and the content for an alert preview.
    """
    out: dict[str, dict] = {}
    for d in deltas:
        tags = d.get("tags") or []
        slug = tag_suffix(tags, "chat:")
        ts = d.get("timestamp") or ""
        if not slug or not ts:
            continue
        existing = out.get(slug)
        if existing is None or ts > (existing.get("timestamp") or ""):
            out[slug] = d
    return out


def _is_incoming(d: dict) -> bool:
    """True for deltas that count as 'a message I might want to see'.

    Excludes the viewer's own typing, ephemeral UI events, and session
    bookkeeping. Used by both list_alerts and mark_all_read so the
    'unread' set they reason about is identical.
    """
    tags = d.get("tags") or []
    if "participant:user" in tags:
        return False  # the viewer's own message
    if "chat-event" in tags:
        return False  # ephemeral UI signal, not a real message
    if "chat-deleted" in tags or "chat-name" in tags:
        return False  # session bookkeeping, not chat content
    return True


async def _compute_unread(viewer: str, since: str) -> list[dict]:
    """Return the alert list for `viewer` — the same shape list_alerts
    returns, factored out so mark_all_read can reuse the detection.
    """
    sessions = await db.list_sessions(limit=200, contact_slug=viewer)
    if not sessions:
        return []

    incoming, views = await asyncio.gather(
        delta_client.query(
            tags_include=["fathom-chat", f"contact:{viewer}"],
            time_start=since,
            limit=_ALERT_QUERY_LIMIT,
        ),
        delta_client.query(
            tags_include=["chat-view", f"contact:{viewer}"],
            time_start=since,
            limit=_ALERT_QUERY_LIMIT,
        ),
    )

    incoming_filtered = [d for d in incoming if _is_incoming(d)]
    incoming_latest = _latest_by_slug(incoming_filtered)
    view_latest = _latest_by_slug(views)

    alerts = []
    for s in sessions:
        slug = s.get("id")
        if not slug:
            continue
        delta = incoming_latest.get(slug)
        if not delta:
            continue
        addr_ts = delta.get("timestamp") or ""
        seen_ts = (view_latest.get(slug) or {}).get("timestamp") or ""
        if addr_ts <= seen_ts:
            continue
        body = (delta.get("content") or "").strip()
        preview = body[:140] if body else (s.get("preview") or "")
        alerts.append(
            {
                "session_id": slug,
                "title": s.get("title") or slug,
                "preview": preview,
                "unread_since": addr_ts,
                "updated_at": s.get("updated_at"),
            }
        )

    alerts.sort(key=lambda a: a.get("unread_since") or "", reverse=True)
    return alerts


async def _write_chat_view(viewer: str, session_slug: str, now: datetime) -> str:
    """Stamp a single chat-view delta. Used by both POST /v1/alerts and
    POST /v1/alerts/mark-all-read."""
    expires = (now + timedelta(days=_CHAT_VIEW_TTL_DAYS)).isoformat()
    written = await delta_client.write(
        content=now.isoformat(),
        tags=["chat-view", f"chat:{session_slug}", f"contact:{viewer}"],
        source="consumer-api",
        expires_at=expires,
    )
    return written.get("id") or ""


@router.get("/v1/alerts")
async def list_alerts(request: Request):
    """Sessions where something incoming landed and I haven't looked yet."""
    viewer = auth.current_contact_slug(request)
    if not viewer:
        # No identity — no alerts. The middleware should have stamped a
        # contact already; if it didn't, returning empty is the right
        # graceful path (no 401 spam from a polling header).
        return {"viewer": None, "count": 0, "alerts": []}

    since = (datetime.now(UTC) - timedelta(days=_ALERT_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    alerts = await _compute_unread(viewer, since)
    return {"viewer": viewer, "count": len(alerts), "alerts": alerts}


class MarkReadRequest(BaseModel):
    session: str


@router.post("/v1/alerts")
async def mark_read(req: MarkReadRequest, request: Request):
    """Stamp a chat-view delta so this session drops out of /v1/alerts.

    Each call produces a fresh delta whose timestamp captures when the
    view happened. The query side takes the latest, so any newer call
    wins. Content carries the ISO timestamp explicitly: delta-store
    dedupes on (content, tags, source), so without a varying field
    repeated mark-reads collapse into a single delta whose timestamp
    is frozen at first-mark — and stale chat-views fail to clear new
    activity.
    """
    viewer = auth.current_contact_slug(request)
    if not viewer:
        raise HTTPException(status_code=401, detail="no contact identity")
    slug = (req.session or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="session is required")

    now = datetime.now(UTC)
    delta_id = await _write_chat_view(viewer, slug, now)
    return {
        "ok": True,
        "session_id": slug,
        "viewer": viewer,
        "id": delta_id,
        "viewed_at": now.isoformat(),
    }


@router.post("/v1/alerts/mark-all-read")
async def mark_all_read(request: Request):
    """One chat-view delta per currently-unread session.

    Bounded by the alert window (30 days) and session list cap (200), so
    the worst case is writing ~200 small deltas — each is just an ISO
    timestamp tagged for one session. Each chat-view TTLs in 35 days, so
    nothing accumulates indefinitely; only the latest per (viewer,
    session) matters at query time.

    Idempotent: a session already viewed past its latest incoming delta
    isn't in the unread set, so it doesn't get a redundant marker.
    Concurrent mark-all-read calls produce some duplicate writes but the
    query side still picks the latest, so correctness holds.
    """
    viewer = auth.current_contact_slug(request)
    if not viewer:
        raise HTTPException(status_code=401, detail="no contact identity")

    since = (datetime.now(UTC) - timedelta(days=_ALERT_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    alerts = await _compute_unread(viewer, since)
    if not alerts:
        return {"ok": True, "marked": 0, "viewer": viewer}

    now = datetime.now(UTC)
    # Concurrent writes — chat-view deltas are independent (different
    # session tags), so we don't need to serialize. Per-session failures
    # are caught individually so one bad write doesn't abandon the rest.
    results = await asyncio.gather(
        *(_write_chat_view(viewer, a["session_id"], now) for a in alerts),
        return_exceptions=True,
    )
    marked = sum(1 for r in results if not isinstance(r, Exception))
    return {
        "ok": True,
        "marked": marked,
        "viewer": viewer,
        "viewed_at": now.isoformat(),
    }
