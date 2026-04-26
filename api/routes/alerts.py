"""Header alerts — derived from unread chat sessions.

An alert is not its own object; it's a *signal* computed from two
sediment streams in the lake:

  • addressed-to-me deltas — anything tagged `fathom-chat` + `for:<me>`
  • my view markers       — `chat-view` deltas tagged `contact:<me>` +
                            `chat:<slug>`

For each session I'm involved in, the latest addressed-to-me delta vs.
the latest view marker decides unread state. No persistence beyond
deltas; no new schema. The view marker is a tiny per-viewer write that
gets reaped on the same lake retention as everything else.

GET  /v1/alerts          — list unread sessions for the current viewer
POST /v1/alerts          — body {session: slug} marks a session read
"""

from __future__ import annotations

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


@router.get("/v1/alerts")
async def list_alerts(request: Request):
    """Sessions where Fathom reached out to me and I haven't looked yet."""
    viewer = auth.current_contact_slug(request)
    if not viewer:
        # No identity — no alerts. The middleware should have stamped a
        # contact already; if it didn't, returning empty is the right
        # graceful path (no 401 spam from a polling header).
        return {"viewer": None, "count": 0, "alerts": []}

    since = (datetime.now(UTC) - timedelta(days=_ALERT_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    # Sessions involving the viewer — same shape the sidebar uses, so
    # title and preview render consistently.
    sessions = await db.list_sessions(limit=200, contact_slug=viewer)
    if not sessions:
        return {"viewer": viewer, "count": 0, "alerts": []}

    addressed = await delta_client.query(
        tags_include=["fathom-chat", f"for:{viewer}"],
        time_start=since,
        limit=_ALERT_QUERY_LIMIT,
    )
    views = await delta_client.query(
        tags_include=["chat-view", f"contact:{viewer}"],
        time_start=since,
        limit=_ALERT_QUERY_LIMIT,
    )

    addressed_latest = _latest_by_slug(addressed)
    view_latest = _latest_by_slug(views)

    alerts = []
    for s in sessions:
        slug = s.get("id")
        if not slug:
            continue
        addr_delta = addressed_latest.get(slug)
        if not addr_delta:
            continue
        addr_ts = addr_delta.get("timestamp") or ""
        seen_ts = (view_latest.get(slug) or {}).get("timestamp") or ""
        if addr_ts <= seen_ts:
            continue
        # Prefer the body of the latest addressed-to-me delta as the
        # preview — that's what the user is being alerted about. Fall
        # back to the session aggregate's preview (which keys on user
        # deltas) only when there's no fathom-written content. 140
        # chars matches the session list aggregation cap.
        body = (addr_delta.get("content") or "").strip()
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
    expires = (now + timedelta(days=_CHAT_VIEW_TTL_DAYS)).isoformat()
    now_iso = now.isoformat()
    written = await delta_client.write(
        content=now_iso,
        tags=["chat-view", f"chat:{slug}", f"contact:{viewer}"],
        source="consumer-api",
        expires_at=expires,
    )
    return {
        "ok": True,
        "session_id": slug,
        "viewer": viewer,
        "id": written.get("id"),
        "viewed_at": now_iso,
    }
