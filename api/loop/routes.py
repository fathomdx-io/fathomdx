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

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import delta_client
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


@router.get("/v1/puddle/deltas")
def get_deltas(
    tags_include: list[str] | None = None,
    tags_exclude: list[str] | None = None,
    time_start: str | None = None,
    time_end: str | None = None,
    limit: int = 1000,
) -> list[dict]:
    """Delta-store-shaped query over the puddle.

    The Grand Loop viz issues `/deltas?tags_include=convo:grand` (and
    similar tag queries) against the experiment's delta-store. This
    endpoint exposes the same shape over the in-memory puddle so the
    viz can be lifted with near-zero adaptation. Returns the bare list
    of delta dicts the viz expects.
    """
    return puddle.query(
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        time_start=time_start,
        time_end=time_end,
        limit=limit,
    )


class EngageRequest(BaseModel):
    kind: str = "more"  # more | less | chat


@router.post("/v1/puddle/cards/{card_id}/engage")
async def engage_card(card_id: str, req: EngageRequest) -> dict:
    """Engagement promotes an ephemeral puddle card to the durable lake.

    The grand-loop output is short-lived by design — anything not engaged
    with TTL-fades into nothing. Engagement is the "authoring" act: the
    user signals this card is worth keeping, and we re-emit its content
    to the lake as a durable `feed-card` delta with the engagement kind
    attached. No engagement → the card disappears with the puddle.
    """
    kind = req.kind.strip().lower()
    if kind not in ("more", "less", "chat"):
        kind = "more"

    card = puddle.get(card_id)
    if card is None:
        # Either expired, never existed, or already TTL'd. Returning 404
        # is design-correct — engaging on a card that's gone shouldn't
        # silently succeed.
        raise HTTPException(status_code=404, detail="card not in puddle (expired or unknown)")

    try:
        payload = json.loads(card.get("content") or "{}")
    except Exception:
        payload = {"body": card.get("content") or ""}

    # Resurrect the route from the puddle card's tags so the durable
    # feed-card carries the same routing intent (chat-reply / drift /
    # bridging / reflection / alert).
    route = "chat-reply"
    for t in card.get("tags") or []:
        if t.startswith("route:"):
            route = t.split(":", 1)[1]
            break

    # Write content + structure as a durable lake feed-card delta. No
    # expires_at — engagement IS the persistence signal.
    durable_tags = [
        "feed-card",
        "promoted-from-puddle",
        f"route:{route}",
        f"engagement:{kind}",
        f"source-card:{card_id}",
    ]
    durable_content = json.dumps(payload, ensure_ascii=False)
    promoted = await delta_client.write(
        content=durable_content,
        tags=durable_tags,
        source="loop-engagement",
    )

    # Mirror the engagement delta in the same shape the existing
    # /v1/feed/engagement endpoint writes, so confidence-scoring and
    # crystal regen still see the signal even though the card came
    # from the loop rather than the old feed pipeline.
    promoted_id = (
        promoted.get("id")
        if isinstance(promoted, dict)
        else (promoted if isinstance(promoted, str) else "")
    )
    await delta_client.write(
        content=(payload.get("body") or "")[:200],
        tags=[
            "feed-engagement",
            f"engagement:{kind}",
            f"card:{promoted_id}" if promoted_id else "card:unknown",
            "via:loop",
        ],
        source="loop-engagement",
    )

    return {
        "ok": True,
        "promoted_card_id": promoted_id,
        "route": route,
        "kind": kind,
    }


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
