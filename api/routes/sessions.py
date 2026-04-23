"""Session CRUD endpoints.

A session groups chat messages. The chat-completions endpoint reads/writes
through `db.*`; these endpoints are the sidebar CRUD surface.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import auth, db

router = APIRouter()


class SessionCreate(BaseModel):
    title: str = "New session"


class SessionUpdate(BaseModel):
    title: str


@router.post("/v1/sessions")
async def create_session(req: SessionCreate):
    return await db.create_session(req.title)


@router.get("/v1/sessions")
async def list_sessions(request: Request, limit: int = 50):
    # Members see only sessions they participated in. Admins see every
    # session so they can support other contacts. Auth gate upstream
    # ensures request.state.contact is always populated when auth is
    # enforced; first-run installs fall through to the default admin.
    slug = auth.current_contact_slug(request)
    contact = getattr(request.state, "contact", None) or {}
    filter_slug = None if contact.get("role") == "admin" else slug
    sessions = await db.list_sessions(limit, contact_slug=filter_slug)
    # Group by recency for the sidebar
    now = datetime.now(UTC)
    groups: dict[str, list] = {"today": [], "yesterday": [], "last_7_days": [], "older": []}
    for s in sessions:
        raw = s["updated_at"]
        try:
            parsed = raw if hasattr(raw, "date") else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            delta_days = (now.date() - parsed.date()).days
        except (ValueError, TypeError):
            delta_days = 999
        if delta_days == 0:
            groups["today"].append(s)
        elif delta_days == 1:
            groups["yesterday"].append(s)
        elif delta_days <= 7:
            groups["last_7_days"].append(s)
        else:
            groups["older"].append(s)
    # Serialize datetimes
    for group in groups.values():
        for s in group:
            for k in ("created_at", "updated_at"):
                if hasattr(s.get(k), "isoformat"):
                    s[k] = s[k].isoformat()
    return {"groups": groups}


@router.get("/v1/sessions/{session_id}")
async def get_session(session_id: str):
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    messages = await db.get_messages(session_id)
    for k in ("created_at", "updated_at"):
        if hasattr(session.get(k), "isoformat"):
            session[k] = session[k].isoformat()
    return {"session": session, "messages": messages}


@router.patch("/v1/sessions/{session_id}")
async def update_session(session_id: str, req: SessionUpdate):
    result = await db.update_session(session_id, req.title)
    if not result:
        raise HTTPException(status_code=404, detail="session not found")
    for k in ("created_at", "updated_at"):
        if hasattr(result.get(k), "isoformat"):
            result[k] = result[k].isoformat()
    return result


@router.delete("/v1/sessions/{session_id}")
async def delete_session(session_id: str):
    deleted = await db.delete_session(session_id)
    return {"deleted": deleted}
