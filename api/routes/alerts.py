"""Header alerts — derived from the Grand Loop puddle.

An alert is not its own object; it's a *signal* computed from witness
output cards in the puddle. The witness picks one of several routes per
fire (chat-reply, feed-card, dm:<slug>, alert:<level>, …); for the
header bell we surface the two that genuinely interrupt:

  · `route:alert:*`  — piercing/warn/info alerts. The witness emits this
                       when urgency outranks normal cadence.
  · `route:dm:*`     — direct messages addressed to a specific contact
                       (the active viewer included).

Read state is stored per-viewer as `alert-viewed-at` deltas in the
durable lake. The latest one wins; alerts in the puddle older than that
timestamp drop out of the unread set. Lake (not puddle) so the receipt
survives api restarts even though the puddle is ephemeral.

GET  /v1/alerts                — list unread alert/message cards
POST /v1/alerts                — body {session: slug} marks read
POST /v1/alerts/mark-all-read  — bumps the viewer's read timestamp
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import auth, delta_client
from ..loop.intents import CONVO_TAG
from ..loop.puddle import puddle

router = APIRouter()


_READ_RECEIPT_TAG = "alert-viewed-at"

# Provenance auto-approve cuts off at L2; L3 (eras) and higher are the
# items that surface here for operator decision. L0/L1/L2 land silently.
_PROPOSAL_MIN_LEVEL = 3

# Non-provenance proposal tools always require operator decision —
# helper-dispatch can do anything on a host machine; mint-routine
# schedules a recurring action. There's no auto-approve for these.
_OPERATOR_REVIEW_TOOLS = {"helper-dispatch", "routines"}


def _is_alert_card(d: dict) -> bool:
    """True for puddle deltas the bell should surface.

    Witness cards are tagged `feed-card` + `route:<value>`. We surface
    the two interrupt routes (alert:* and dm:*); chat-reply / feed-card
    / unknown stay quiet — they're the ambient stream, not interruptions.
    """
    tags = d.get("tags") or []
    if "feed-card" not in tags:
        return False
    for t in tags:
        if t.startswith("route:alert:") or t.startswith("route:dm:"):
            return True
    return False


def _route_kind(tags: list[str]) -> str:
    """`alert` or `message` — extracted from the route tag for UI grouping."""
    for t in tags:
        if t.startswith("route:alert:"):
            return "alert"
        if t.startswith("route:dm:"):
            return "message"
    return "alert"


def _card_preview(d: dict) -> tuple[str, str]:
    """(title, preview) extracted from a witness JSON payload."""
    raw = (d.get("content") or "").strip()
    title = ""
    body = ""
    if raw:
        try:
            payload = json.loads(raw)
            title = (payload.get("title") or payload.get("kicker") or "").strip()
            body = (payload.get("body") or "").strip()
        except Exception:
            body = raw
    preview = body[:140] if body else title
    return title or "Alert", preview


def _provenance_level(tags: list[str]) -> int | None:
    """Read `provenance-level:<n>` off a proposal's tags, or None."""
    for t in tags or []:
        if isinstance(t, str) and t.startswith("provenance-level:"):
            try:
                return int(t.split(":", 1)[1])
            except (TypeError, ValueError):
                continue
    return None


def _proposal_preview(
    d: dict,
    *,
    tool: str = "",
    level: int | None = None,
) -> tuple[str, str]:
    """(title, preview) for a kind:proposal delta.

    Tool-aware title prefix:
      · provenance       → "L<n> proposal: <title>"
      · helper-dispatch  → "Helper on <host>: <title>"
      · routines         → "Mint routine: <name>"
      · other            → "Proposal: <title>"
    """
    raw = (d.get("content") or "").strip()
    title = ""
    preview = ""
    constituents = 0
    args: dict = {}
    payload: dict = {}
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                args = payload.get("tool_args") or {}
                if not isinstance(args, dict):
                    args = {}
                title = (args.get("title") or "").strip()
                rationale = (args.get("rationale") or "").strip()
                summary = (args.get("summary") or "").strip()
                preview = rationale or summary
                from_ids = args.get("from_ids") or []
                if isinstance(from_ids, list):
                    constituents = len(from_ids)
                if not title:
                    title = (payload.get("title") or payload.get("kicker") or "").strip()
                if not preview:
                    preview = (payload.get("body") or "").strip()
        except Exception:
            preview = raw

    if tool == "provenance":
        prefix = f"L{level} proposal" if level is not None else "Proposal"
        title_text = f"{prefix}: {title}" if title else prefix
        if constituents:
            preview = f"({constituents} constituents) {preview}"
    elif tool == "helper-dispatch":
        host = (args.get("host") or "").strip()
        task = (args.get("task") or "").strip()
        title_text = f"Helper on {host}: {title or task[:60]}" if host else f"Helper: {title or task[:60]}"
        preview = task or preview
    elif tool == "routines":
        name = (args.get("name") or "").strip()
        schedule = (args.get("schedule") or "").strip()
        prompt = (args.get("prompt") or "").strip()
        title_text = f"Mint routine: {name or title}"
        sched_part = f"[{schedule}] " if schedule else ""
        preview = f"{sched_part}{prompt or preview}"
    else:
        title_text = f"Proposal: {title}" if title else "Proposal"

    return title_text, (preview or "")[:200]


def _proposal_tool(tags: list[str]) -> str:
    """Read `tool:<name>` off a proposal's tags."""
    for t in tags or []:
        if isinstance(t, str) and t.startswith("tool:"):
            return t.split(":", 1)[1]
    return ""


async def _pending_proposal_alerts() -> list[dict]:
    """Query the lake for pending proposals that need operator attention.

    Surfaces:
      · L3+ provenance proposals (L1/L2 auto-approve and never reach here)
      · helper-dispatch proposals (always operator-gated — these run
        claude-code on a host machine)
      · routine-mint proposals (always operator-gated — these schedule
        recurring actions)

    Filters out anything with an existing decision delta.
    """
    try:
        proposals = await delta_client.query(
            tags_include=["kind:proposal", "proposal-status:pending"],
            limit=50,
        )
    except Exception:
        return []
    out: list[dict] = []
    for p in proposals:
        tags = p.get("tags") or []
        tool = _proposal_tool(tags)
        level = _provenance_level(tags)
        # Tool-kind gating:
        #   provenance → only surface L3+ (L1/L2 auto-approve elsewhere)
        #   helper-dispatch / routines → always surface (always operator-gated)
        #   anything else → ignore (unknown tool, conservative skip)
        if tool == "provenance":
            if level is None or level < _PROPOSAL_MIN_LEVEL:
                continue
        elif tool in _OPERATOR_REVIEW_TOOLS:
            pass
        else:
            continue
        delta_id = p.get("id") or ""
        if not delta_id:
            continue
        # Filter out anything that already has a decision recorded.
        try:
            decisions = await delta_client.query(
                tags_include=[f"decides:{delta_id}"],
                limit=1,
            )
        except Exception:
            decisions = []
        if decisions:
            continue
        title, preview = _proposal_preview(p, tool=tool, level=level)
        ts = p.get("timestamp") or ""
        out.append(
            {
                "session_id": delta_id,
                "delta_id": delta_id,
                "title": title,
                "preview": preview,
                "unread_since": ts,
                "updated_at": ts,
                "kind": "proposal",
                "tool": tool,
                "level": level,
            }
        )
    return out


async def _latest_viewed_at(viewer: str) -> str:
    """Most recent `alert-viewed-at` ISO timestamp for this viewer, or ''."""
    try:
        items = await delta_client.query(
            tags_include=[_READ_RECEIPT_TAG, f"contact:{viewer}"],
            limit=1,
        )
    except Exception:
        return ""
    if not items:
        return ""
    return (items[0].get("content") or "").strip() or (items[0].get("timestamp") or "")


def _unread_alerts(deltas: list[dict], viewed_at: str) -> list[dict]:
    """Filter alert/message cards to those newer than the viewer's
    last read receipt."""
    out: list[dict] = []
    for d in deltas:
        if not _is_alert_card(d):
            continue
        ts = d.get("timestamp") or ""
        if viewed_at and ts <= viewed_at:
            continue
        title, preview = _card_preview(d)
        out.append(
            {
                "session_id": d.get("id") or "",
                "title": title,
                "preview": preview,
                "unread_since": ts,
                "updated_at": ts,
                "kind": _route_kind(d.get("tags") or []),
            }
        )
    out.sort(key=lambda a: a.get("unread_since") or "", reverse=True)
    return out


async def _write_viewed_at(viewer: str, now: datetime) -> str:
    """Stamp the viewer's latest read timestamp into the durable lake."""
    written = await delta_client.write(
        content=now.isoformat(),
        tags=[_READ_RECEIPT_TAG, f"contact:{viewer}"],
        source="fathom-engagement",
    )
    return written.get("id") or ""


@router.get("/v1/alerts")
async def list_alerts(request: Request):
    """Witness cards in the puddle the viewer hasn't acknowledged yet."""
    viewer = auth.current_contact_slug(request)
    if not viewer:
        # No identity — the middleware should have stamped a contact;
        # if it didn't, returning empty is the right graceful path
        # (no 401 spam from a polling header).
        return {"viewer": None, "count": 0, "alerts": []}

    deltas = puddle.query(tags_include=[CONVO_TAG, "feed-card"], limit=500)
    viewed_at = await _latest_viewed_at(viewer)
    alerts = _unread_alerts(deltas, viewed_at)
    # L3+ provenance proposals are bell-worthy regardless of viewed_at —
    # they stay until the operator approves or denies. Mark-all-read
    # only clears the timestamp-driven alerts (those age past viewed_at);
    # proposals are decision-driven, not read-driven.
    proposal_alerts = await _pending_proposal_alerts()
    merged = proposal_alerts + alerts
    merged.sort(key=lambda a: a.get("unread_since") or "", reverse=True)
    return {"viewer": viewer, "count": len(merged), "alerts": merged}


class MarkReadRequest(BaseModel):
    session: str | None = None


@router.post("/v1/alerts")
async def mark_read(req: MarkReadRequest, request: Request):
    """Bump the viewer's read timestamp.

    Read state is now global (single latest-viewed-at per viewer). The
    `session` field is accepted for backward UI compatibility but
    ignored — alerts come from the puddle, not chat sessions.
    """
    viewer = auth.current_contact_slug(request)
    if not viewer:
        raise HTTPException(status_code=401, detail="no contact identity")

    now = datetime.now(UTC)
    delta_id = await _write_viewed_at(viewer, now)
    return {
        "ok": True,
        "session_id": req.session or "",
        "viewer": viewer,
        "id": delta_id,
        "viewed_at": now.isoformat(),
    }


@router.post("/v1/alerts/mark-all-read")
async def mark_all_read(request: Request):
    """Bump the viewer's read timestamp for every currently-unread card.

    Same single-write shape as POST /v1/alerts — global read state, one
    receipt per call. The marked count is reported for parity with the
    old per-session behaviour, computed as the unread count at the
    moment of the call.
    """
    viewer = auth.current_contact_slug(request)
    if not viewer:
        raise HTTPException(status_code=401, detail="no contact identity")

    deltas = puddle.query(tags_include=[CONVO_TAG, "feed-card"], limit=500)
    viewed_at = await _latest_viewed_at(viewer)
    unread = _unread_alerts(deltas, viewed_at)

    now = datetime.now(UTC)
    await _write_viewed_at(viewer, now)
    return {
        "ok": True,
        "marked": len(unread),
        "viewer": viewer,
        "viewed_at": now.isoformat(),
    }
