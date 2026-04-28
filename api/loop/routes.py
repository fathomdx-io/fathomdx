"""HTTP surface for the Grand Loop — composer writes, dashboard reads.

Three groups of endpoints:

  Composer side (write):
    POST /v1/puddle/seed     — drop a seed delta (kind:question by default)

  Dashboard side (read):
    GET  /v1/puddle/cards    — feed-card witness outputs currently alive
    GET  /v1/puddle/intents  — pending intent queue (key UI element)
    GET  /v1/puddle/stream   — SSE: every puddle write fans out here
    GET  /v1/puddle/stats    — quick health snapshot

Engagement (promote-to-lake) lands in a follow-up commit — it crosses
the puddle/lake boundary and wants the existing engagement-delta path
in api/routes/feed.py to keep working with whatever shape we settle on.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .intents import (
    CONVO_TAG,
    INTENT_TTL_BY_KIND,
    intent_kind,
    pending_intents,
    write_intent,
)
from .puddle import puddle


router = APIRouter()


class SeedRequest(BaseModel):
    content: str
    kind: str = "question"
    extra_tags: list[str] | None = None


@router.post("/v1/puddle/seed")
async def post_seed(req: SeedRequest) -> dict:
    """Drop a seed delta into the puddle.

    The composer at the bottom of the dashboard hits this endpoint. The
    seed appears in the public stream (Grand Loop is open-forum by
    design), enters the intent queue, and gets picked up by the next
    supervisor tick.
    """
    kind = req.kind.strip().lower() or "question"
    if kind not in INTENT_TTL_BY_KIND:
        kind = "question"
    delta = await write_intent(
        kind=kind,
        content=req.content.strip(),
        extra_tags=req.extra_tags,
        source="composer",
    )
    return {"ok": True, "intent": delta}


@router.get("/v1/puddle/cards")
def get_cards(limit: int = 50) -> dict:
    """Witness outputs (feed-card synthesis deltas) alive in the puddle.

    Each card's `content` is JSON with kicker/title/body/tail/route/axes.
    The dashboard parses and renders. Newest-first.
    """
    raw = puddle.query(
        tags_include=[CONVO_TAG, "feed-card"],
        limit=limit,
    )
    cards = []
    for d in raw:
        try:
            payload = json.loads(d.get("content") or "{}")
        except Exception:
            payload = {"body": d.get("content") or ""}
        addressed = [
            t.split(":", 1)[1]
            for t in (d.get("tags") or [])
            if t.startswith("addresses:")
        ]
        cards.append({
            "id": d.get("id"),
            "timestamp": d.get("timestamp"),
            "expires_at": d.get("expires_at"),
            "addresses": addressed,
            **payload,
        })
    return {"cards": cards}


@router.get("/v1/puddle/intents")
def get_intents() -> dict:
    """Pending intent queue. The 'what's next' indicator the user
    asked for prominent UI on."""
    pending = pending_intents()
    out = []
    for d in pending:
        out.append({
            "id": d.get("id"),
            "kind": intent_kind(d),
            "content": d.get("content") or "",
            "timestamp": d.get("timestamp"),
            "expires_at": d.get("expires_at"),
        })
    return {"intents": out}


@router.get("/v1/puddle/stats")
def get_stats() -> dict:
    return puddle.stats()


@router.get("/v1/puddle/_debug")
def debug_dump(limit: int = 50) -> dict:
    """Temporary: dump all alive deltas with tags for spike-mode debugging."""
    raw = puddle.query(limit=limit)
    return {
        "deltas": [
            {
                "id": d.get("id"),
                "tags": d.get("tags"),
                "source": d.get("source"),
                "timestamp": d.get("timestamp"),
                "expires_at": d.get("expires_at"),
                "content_preview": (d.get("content") or "")[:120],
            }
            for d in raw
        ],
    }


@router.get("/v1/puddle/stream")
async def stream() -> StreamingResponse:
    """Server-Sent Events — one event per puddle write.

    Used by the live viz and (eventually) the main dashboard view to
    push cards, voice thoughts, and process events without polling.
    """
    async def event_gen():
        async for delta in puddle.subscribe(maxsize=256):
            try:
                yield f"data: {json.dumps(delta)}\n\n"
            except Exception:
                # Defensive: if a delta carries something json.dumps can't
                # encode, skip rather than killing the stream for everyone.
                continue

    return StreamingResponse(event_gen(), media_type="text/event-stream")
