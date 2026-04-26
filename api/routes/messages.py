"""POST /v1/messages — send a message to a contact.

The HTTP face of `messages.send_message`. Used by MCP clients (via the
LAKE_TOOLS registry) and CLI. The chat-LLM dispatch path goes through
`tools.execute()` directly — same underlying helper, different framing.

Authorship is always Fathom for now. The caller supplies a recipient
(or omits it to default to themselves); the writer tag is fixed at
`participant:fathom`. That fits the "Fathom reaches out" frame even
when a human triggers the call from MCP — Fathom is the messenger.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import messages as messages_mod

router = APIRouter()


class MessageRequest(BaseModel):
    body: str
    to: str | None = None
    session: str | None = None


@router.post("/v1/messages")
async def post_message(req: MessageRequest, request: Request):
    contact = getattr(request.state, "contact", None) or {}
    caller_slug = contact.get("slug")

    recipient = (req.to or "").strip() or caller_slug
    if not recipient:
        raise HTTPException(
            status_code=400,
            detail=(
                "no recipient — pass `to` with a contact slug, or call with "
                "an authenticated token so the caller's slug can default"
            ),
        )

    body = (req.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="body is required")

    try:
        return await messages_mod.send_message(
            recipient_slug=recipient,
            body=body,
            writer_slug="fathom",
            session_slug=(req.session or None),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
