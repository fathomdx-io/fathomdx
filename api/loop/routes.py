"""HTTP surface for the Grand Loop — composer writes, dashboard reads.

Endpoint groups:

  Composer side (write):
    POST /v1/puddle/seed                — drop a seed (dual-write
                                          puddle + lake; user-authored
                                          is durable from the moment
                                          of typing)
    POST /v1/puddle/cards/{id}/engage   — author an engaged card into
                                          the lake (more / less / chat)

  Dashboard side (read):
    GET  /v1/puddle/cards    — feed-card witness outputs currently alive
    GET  /v1/puddle/intents  — pending intent queue (key UI element)
    GET  /v1/puddle/feed     — chronological unified stream
    GET  /v1/puddle/stream   — SSE: every puddle write fans out here
    GET  /v1/puddle/stats    — quick health snapshot
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
    Q_A_TTL_S,
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
    """Drop a seed delta into the puddle AND the lake.

    Typing a seed is authoring — the lake gets it durable from the
    moment of writing. The puddle gets the immediate echo so the
    loop's next round and the dashboard's feed see it without waiting
    for telepathy to reflect it back. The puddle copy carries
    `lake-id:<full>` and `recalled-id:<24chars>` so telepathy
    correctly dedupes (composer is also in MIRROR_NOISE_SOURCES, so
    the filter and the tag are belt-and-suspenders here).

    Lake write is best-effort: a transient lake hiccup must not block
    the user from sending. The puddle write always lands.
    """
    kind = req.kind.strip().lower() or "question"
    if kind not in INTENT_TTL_BY_KIND:
        kind = "question"
    body = req.content.strip()

    lake_tags = ["user-seed", f"kind:{kind}"]
    if req.extra_tags:
        lake_tags.extend(req.extra_tags)
    lake_id = ""
    try:
        lake_delta = await delta_client.write(
            content=body,
            tags=lake_tags,
            source="composer",
        )
        if isinstance(lake_delta, dict):
            lake_id = lake_delta.get("id") or ""
    except Exception as e:
        print(f"[seed] lake write failed (puddle still writing): {type(e).__name__}: {e}")

    puddle_extra: list[str] = list(req.extra_tags or [])
    if lake_id:
        puddle_extra.append(f"lake-id:{lake_id}")
        puddle_extra.append(f"recalled-id:{lake_id[:24]}")
    delta = await write_intent(
        kind=kind,
        content=body,
        extra_tags=puddle_extra,
        source="composer",
    )
    return {"ok": True, "intent": delta, "lake_id": lake_id}


@router.get("/v1/puddle/feed")
def get_feed(
    until: str | None = None,
    hours: float = 1.0,
    limit: int = 500,
) -> dict:
    """Unified feed: user seeds + fathom replies + witness cards in one
    chronological list, newest first.

    The dashboard renders three component shapes from this one stream:
      * kind=user-message   → renderTurn({role:'user'})    .msg-user
      * kind=fathom-message → renderTurn({role:'fathom'})  .msg-fathom
      * kind=card           → renderStoryCard()            .card

    A user's seed (kind:question intent) appears as user-message regardless
    of whether the witness has addressed it yet — leaving the seed visible
    after its reply lands keeps the conversation legible. The witness
    output's `addresses` list points back to the seed it answered.
    """
    # Time-windowed pagination: each page is the last `hours` of the
    # puddle counted backward from `until` (defaults to now). The dashboard
    # walks back one hour at a time on Show-more. Lots of different item
    # kinds can land in any window — voice thoughts, recalls, intents,
    # cards — so paging by time gives a stable rhythm; paging by item
    # count would mean "show more" pulls inconsistent slices when the
    # puddle is dense.
    from datetime import datetime, timedelta, UTC

    until_dt = (
        datetime.fromisoformat(until.replace("Z", "+00:00"))
        if until else datetime.now(UTC)
    )
    since_dt = until_dt - timedelta(hours=hours)
    until_iso = until_dt.isoformat()
    since_iso = since_dt.isoformat()

    items: list[dict] = []
    raw = puddle.query(
        tags_include=[CONVO_TAG],
        time_start=since_iso,
        time_end=until_iso,
        limit=limit,
    )

    for d in raw:
        tags = set(d.get("tags") or [])
        ts = d.get("timestamp")
        # Pull the session tag out so the frontend can cluster items by
        # (kind, session) — that way all voices from one deliberation
        # pile into a single accordion even when recall results
        # interleave them chronologically.
        session_tag = next(
            (t for t in tags if t.startswith("session:")),
            None,
        )
        common = {
            "id": d.get("id"),
            "timestamp": ts,
            "expires_at": d.get("expires_at"),
            "source": d.get("source"),
            "tags": list(d.get("tags") or []),
            "session": session_tag,
        }

        # ── Q (always visible by default) ─────────────────
        # Match both vocabularies: `intent`+`kind:question` is the
        # puddle-native shape (write_intent), `user-seed`+`kind:question`
        # is the lake-side shape (post_seed's durable write). Telepathy
        # may surface the lake shape directly when restoring on cold-
        # start, and the renderer should treat both as the same kind.
        if "kind:question" in tags and ("intent" in tags or "user-seed" in tags):
            items.append({
                "kind": "user-message",
                "content": d.get("content") or "",
                **common,
            })
            continue

        # ── A — witness output, split by route ────────────
        if "feed-card" in tags:
            try:
                payload = json.loads(d.get("content") or "{}")
            except Exception:
                payload = {"body": d.get("content") or ""}
            route = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("route:")),
                "chat-reply",
            )
            addresses = [t.split(":", 1)[1] for t in tags if t.startswith("addresses:")]
            base = {
                **common,
                "addresses": addresses,
                "route": route,
                **payload,
            }
            if route == "chat-reply":
                items.append({"kind": "fathom-message", **base})
            else:
                items.append({"kind": "card", **base})
            continue

        # ── Auxiliary types (filter-toggleable) ───────────
        # Voice thoughts — the parliament's individual takes.
        if "thought" in tags:
            voice = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("voice:")),
                None,
            )
            items.append({
                "kind": "voice-thought",
                "voice": voice,
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Pulse intents (reflection / drift / bridging / alert) — these
        # are pressure-watcher pass intents that haven't been addressed
        # by witness yet. Surface them so the queue is visible.
        if "intent" in tags:
            kind = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("kind:")),
                "unknown",
            )
            if kind in ("reflection", "drift", "bridging", "alert", "drop-in"):
                items.append({
                    "kind": "pass-intent",
                    "pass_kind": kind,
                    "content": d.get("content") or "",
                    **common,
                })
            continue
        # Crystal facets — identity layer.
        if "crystal" in tags:
            facet = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("facet:")),
                None,
            )
            items.append({
                "kind": "crystal",
                "facet": facet,
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Mood / felt-sense.
        if "mood" in tags:
            feeling = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("feeling:")),
                None,
            )
            items.append({
                "kind": "mood",
                "feeling": feeling,
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Routine activity — fires (the trigger) and summaries (the
        # writeup the routine produced). Both surface under one kind so
        # the filter can toggle them independently of generic lake-delta
        # noise; the `summary` field lets the renderer tell them apart.
        if "routine-fire" in tags or "routine-summary" in tags:
            routine_id = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("routine-id:")),
                None,
            )
            items.append({
                "kind": "routine",
                "routine_id": routine_id,
                "summary": "routine-summary" in tags,
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Lake delta — telepathy mirror of recent durable lake activity
        # (RSS arrivals, claude-code session deltas, anything new in the
        # lake that isn't loop-output noise). Surfaced under its own kind
        # so the filter can show "raw lake" separately from model-driven
        # recall results.
        if "lake-delta" in tags:
            from_source = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("from-source:")),
                None,
            )
            items.append({
                "kind": "lake-delta",
                "from_source": from_source,
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Compositional recall results — present and future model-driven
        # recall pulls (separate from the continuous mirror above).
        if "recall-result" in tags or "mirror" in tags:
            items.append({
                "kind": "recall",
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Process events (spawn/die/metric) are no longer written by
        # process.py — they were interleaving between voice thoughts in
        # the feed and breaking the cluster. Defensive: silently drop any
        # legacy process-event still alive in the puddle from before the
        # change. They TTL out on their own.
        if "process-event" in tags or "metric" in tags:
            continue

    items.sort(key=lambda it: it.get("timestamp") or "", reverse=True)
    # has_more is true if the puddle holds anything older than the
    # window's `since` boundary. One cheap query, since=None,
    # time_end=since: any hit means there's more to page through.
    older = puddle.query(
        tags_include=[CONVO_TAG],
        time_end=since_iso,
        limit=1,
    )
    return {
        "items": items,
        "since": since_iso,
        "until": until_iso,
        "hours": hours,
        "has_more": bool(older),
    }


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


@router.post("/v1/puddle/pulse")
async def fire_pulse(reason: str = "manual") -> dict:
    """Drop one intent per pass (reflection / drift / bridging / alert)
    into the puddle, same as the pressure-watcher does on a substrate
    pressure crossing. Used to manually trigger a feed synthesis when
    you want one without waiting for ambient pressure to build."""
    from .pressure import fire_pressure_pulse
    await fire_pressure_pulse(reason)
    return {"ok": True, "reason": reason}


@router.get("/v1/puddle/stats")
def get_stats() -> dict:
    return puddle.stats()


@router.get("/v1/puddle/metrics")
def get_metrics(per_voice: int = 8) -> dict:
    """Recent cross-voice convergence samples per voice.

    Drives the dashboard's convergence dots — each dot's x-position
    tracks (1 - distance) of the latest sample for that voice. Returns
    a dict keyed by voice name; each value is a list of {timestamp,
    distance} sorted oldest-first so the renderer can take the tail.
    """
    raw = puddle.query(tags_include=[CONVO_TAG, "metric"], limit=200)
    by_voice: dict[str, list[dict]] = {}
    for d in raw:
        voice = next(
            (t.split(":", 1)[1] for t in (d.get("tags") or []) if t.startswith("voice:")),
            None,
        )
        if not voice:
            continue
        try:
            payload = json.loads(d.get("content") or "{}")
        except Exception:
            continue
        dist = payload.get("distance")
        if not isinstance(dist, (int, float)):
            continue
        by_voice.setdefault(voice, []).append({
            "timestamp": d.get("timestamp"),
            "distance": float(dist),
        })
    out: dict[str, list[dict]] = {}
    for voice, samples in by_voice.items():
        samples.sort(key=lambda s: s.get("timestamp") or "")
        out[voice] = samples[-per_voice:]
    return {"voices": out}


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
    """Author an engagement delta pointing at the card.

    Engagement is its own shape — a `feed-engagement` delta with an
    `engages:<lake_id>` pointer to the card it modifies (matching the
    generalized engagement-as-delta vocabulary documented in
    docs/reference/feed-spec.md and reserved-tags-spec.md). The card
    itself is not duplicated. The dashboard renders the engagement
    state by projecting these markers onto their target cards: card
    + engagement = a card with state, never two cards.

    Dual-write to lake + puddle so the dashboard's puddle watcher sees
    the engagement immediately, without waiting for telepathy's next
    5-minute tick. The puddle copy carries lake-id + recalled-id back-
    references so telepathy correctly dedupes when it later mirrors
    the lake delta.
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

    # The card's lake-id tag is what we'll point at via `engages:`.
    # That's stable across telepathy round-trips: a freshly-written
    # puddle card has `lake-id:<full>`, and a telepathy-restored card
    # has `recalled-id:<short>` (which the frontend matches by prefix).
    card_lake_id = ""
    for t in card.get("tags") or []:
        if t.startswith("lake-id:"):
            card_lake_id = t.split(":", 1)[1]
            break

    # Engagement marker — short content for context, the full pointer
    # carried by the `engages:` tag. via:loop preserves the legacy
    # confidence-scoring/crystal-regen signal shape.
    marker_content = (payload.get("body") or "")[:200]
    marker_tags = [
        "feed-engagement",
        f"engagement:{kind}",
        "via:loop",
    ]
    if card_lake_id:
        marker_tags.append(f"engages:{card_lake_id}")
    marker = await delta_client.write(
        content=marker_content,
        tags=marker_tags,
        source="loop-engagement",
    )
    marker_id = (
        marker.get("id")
        if isinstance(marker, dict)
        else (marker if isinstance(marker, str) else "")
    )
    puddle_marker_tags = [CONVO_TAG, *marker_tags]
    if marker_id:
        puddle_marker_tags.append(f"lake-id:{marker_id}")
        puddle_marker_tags.append(f"recalled-id:{marker_id[:24]}")
    await puddle.write(
        content=marker_content,
        tags=puddle_marker_tags,
        source="loop-engagement",
        ttl_seconds=Q_A_TTL_S,
    )

    return {
        "ok": True,
        "marker_id": marker_id,
        "engages": card_lake_id,
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
