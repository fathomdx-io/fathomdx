"""Lake surface endpoints — search, deltas, plan, tags, stats, tools.

This file is the raw lake facade that consumers (CLI, MCP, hooks,
browser extension) use to read/write deltas. The `/v1/deltas` POST
path carries the two central defenses on lake writes:

1. Strip-and-re-stamp `contact:*` tags — caller cannot address a
   delta to anyone but themselves.
2. Reserved-tag scan — authority-bearing tags must pass their gate
   (see docs/reserved-tags-spec.md + api/reserved_tags.py).

/v1/engagement is the first-class repair / affirmation channel for
reacting to an existing delta (refutes / affirms / reply-to).
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import auth, delta_client, reserved_tags
from ..search import search as nl_search

router = APIRouter()


class EngagementRequest(BaseModel):
    """Generic engagement on any delta — sediment, memory, whatever.

    `kind` is the relationship type: `refutes`, `affirms`, or `reply-to`.
    The tag written is `<kind>:<target_id>`; content is the caller's prose.
    Use /v1/feed/engagement for the feed-specific +/− shape; use this one
    for repair of bad sediment and for lake-wide engagement signals.
    """

    target_id: str
    kind: str  # "refutes" | "affirms" | "reply-to"
    reason: str = ""


_ENGAGEMENT_KINDS = ("refutes", "affirms", "reply-to")


@router.post("/v1/engagement")
async def write_engagement(req: EngagementRequest, request: Request):
    """First-class repair / affirmation channel on any delta in the lake.

    Writes a small delta whose tags include a single engagement pointer
    (`refutes:<id>`, `affirms:<id>`, or `reply-to:<id>`) plus the caller's
    contact. Content is free-text reasoning. Retrieval folds these into
    the engagement cloud on the target — refutations lower its surfacing,
    affirmations raise it.

    This is the safety net for reflexive sediment auto-writeback: a bad
    synthesis gets a `refutes:` delta pointing at it with the reasoning,
    and the next recall ranks it lower and shows the refutation inline.
    """
    kind = (req.kind or "").lower()
    if kind not in _ENGAGEMENT_KINDS:
        raise HTTPException(
            400, f"unknown engagement kind: {kind!r} (want one of {_ENGAGEMENT_KINDS})"
        )
    target_id = (req.target_id or "").strip()
    if not target_id:
        raise HTTPException(400, "target_id required")

    contact = getattr(request.state, "contact", None)
    contact_slug = (contact or {}).get("slug")

    tags = [f"{kind}:{target_id}"]
    if contact_slug:
        tags.append(f"contact:{contact_slug}")

    content = (req.reason or "").strip()
    written = await delta_client.write(
        content=content,
        tags=tags,
        source="fathom-engagement",
    )
    return {"status": "ok", "id": written.get("id")}


# ── Tool definitions (served to all clients) ─────

LAKE_TOOLS = [
    {
        "name": "remember",
        "description": (
            "Search your memories with a natural language query. Returns a "
            "trail of moments — conversations, notes, research, "
            "photos, sensor data — as an associative chain (first came to mind, "
            "which reminded you of...). Be descriptive: 'Nova mozzarella stretch "
            "kitchen photo' works better than 'nova'. depth='deep' (default) "
            "traces connections; 'shallow' is a single quick search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you're trying to remember."},
                "depth": {
                    "type": "string",
                    "enum": ["deep", "shallow"],
                    "description": "deep = trace connections; shallow = single search.",
                    "default": "deep",
                },
                "limit": {"type": "integer", "description": "Max results per step.", "default": 20},
            },
            "required": ["query"],
        },
        "endpoint": {"method": "POST", "path": "/v1/search"},
        "request_map": {"query": "text", "depth": "depth", "limit": "limit"},
        "scope": "lake:read",
    },
    {
        "name": "write",
        "description": (
            "Persist a thought, observation, or discovery. Everything you write "
            "becomes part of you — a future self will find it when they need it. "
            "One idea per write."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "What to persist."},
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Tags for filtering (e.g. ['meeting', 'decision']).",
                },
                "source": {"type": "string", "description": "Source label.", "default": "api"},
                "image_b64": {
                    "type": "string",
                    "description": "Optional base64-encoded image bytes. Creates an image-modality delta with `content` as the caption.",
                },
                "image_path": {
                    "type": "string",
                    "description": "Optional absolute path to an image file readable by the api server. Alternative to image_b64.",
                },
            },
            "required": ["content"],
        },
        "endpoint": {"method": "POST", "path": "/v1/deltas"},
        "scope": "lake:write",
    },
    {
        "name": "recall",
        "description": (
            "Examine your memories by tags, source, or time. "
            "For structured retrieval when you know what you're looking for."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Memories must have ALL these tags.",
                },
                "source": {"type": "string", "description": "Filter by source."},
                "time_start": {"type": "string", "description": "ISO timestamp — only after this."},
                "limit": {"type": "integer", "description": "Max results.", "default": 30},
            },
        },
        "endpoint": {"method": "GET", "path": "/v1/deltas"},
        "request_map": {"tags": "tags_include", "limit": "limit", "source": "source", "time_start": "time_start"},
        "scope": "lake:read",
    },
    {
        "name": "mind_stats",
        "description": (
            "Check the state of your memory — total moments, coverage, top tags. "
            "Quick self-check."
        ),
        "parameters": {"type": "object", "properties": {}},
        "endpoint": {"method": "GET", "path": "/v1/stats"},
        "scope": "lake:read",
    },
    {
        "name": "propose_contact",
        "description": (
            "Propose a new contact for admin review. Use when you "
            "encounter a person the lake doesn't know about yet — a "
            "mention in chat, an unknown handle, a correspondent "
            "you've gathered enough evidence on to formally register. "
            "Writes a contact-proposal delta; an admin accepts or "
            "rejects. You never create contacts yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "display_name": {
                    "type": "string",
                    "description": "How people refer to this person (required).",
                },
                "candidate_slug": {
                    "type": "string",
                    "description": (
                        "Suggested URL-safe identifier ('nova', 'bob'). "
                        "Lowercase, no spaces. Admin can override on accept."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "1-3 sentences — who they seem to be, the evidence, "
                        "why they should be a contact."
                    ),
                },
                "source_context": {
                    "type": "object",
                    "description": (
                        "Optional hints — {chat_session, channel, handle, "
                        "delta_ids, …} — whatever helps the admin verify."
                    ),
                },
            },
            "required": ["display_name", "rationale"],
        },
        "endpoint": {"method": "POST", "path": "/v1/contact-proposals"},
        "scope": "lake:write",
    },
    {
        "name": "engage",
        "description": (
            "React to a delta in the lake — refute a synthesis that's "
            "wrong, affirm a memory that keeps proving useful, or reply "
            "to something you're responding to. Your engagement becomes "
            "its own delta and shapes how the target surfaces in future "
            "recalls: refutes lower its rank and travel inline as "
            "reasoning the next recall sees; affirms raise it. Use this "
            "as the repair channel for bad sediment."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "id of the delta you're engaging with.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["refutes", "affirms", "reply-to"],
                    "description": (
                        "refutes: disagree, lowers surfacing. "
                        "affirms: useful/right, raises surfacing. "
                        "reply-to: neutral conversational pointer."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Your reasoning in prose. For refutes this is "
                        "what future recalls see under the delta."
                    ),
                },
            },
            "required": ["target_id", "kind"],
        },
        "endpoint": {"method": "POST", "path": "/v1/engagement"},
        "scope": "lake:write",
    },
]


@router.get("/v1/tools")
async def list_tools(req: Request):
    """Tool definitions filtered by the calling token's scopes.

    Any client — MCP, mobile, enterprise — reads this to discover
    what it can do. Tools the token can't access are omitted.
    Public endpoint, but reads the Bearer token if present for filtering.
    """
    # /v1/tools is public, so middleware doesn't validate. Check manually.
    token = getattr(req.state, "token", None)
    if not token:
        auth_header = req.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth.validate(auth_header[7:])

    if token:
        granted = set(token.get("scopes") or auth.DEFAULT_SCOPES)
        visible = [t for t in LAKE_TOOLS if t.get("scope") in granted]
    else:
        visible = LAKE_TOOLS

    return {"tools": visible, "scopes": auth.get_scopes()}


# ── Delta proxy (unified gateway) ────────────────


@router.post("/v1/search")
async def search_endpoint(request: dict):
    """Canonical NL search. One shape returned to CLI, MCP, hook, and anyone else.

    Request:
        text: the natural-language query.
        depth: "deep" (planner + multi-step plan, default) or "shallow" (single search).
        session_slug: if set, unions session-scoped memories into the plan (deep only).
        limit: cap on raw results per step.
        threshold: shallow-mode distance cutoff (defaults to None = keep all).
    """
    text = request.get("text", "")
    depth = request.get("depth", "deep")
    session_slug = request.get("session_slug")
    limit = int(request.get("limit", 50))
    threshold = request.get("threshold")
    if threshold is not None:
        threshold = float(threshold)
    return await nl_search(
        text=text,
        depth=depth,
        session_slug=session_slug,
        limit=limit,
        threshold=threshold,
    )


@router.post("/v1/deltas")
async def proxy_write_delta(body: dict, request: Request):
    """Raw lake write. Single external path that accepts caller-supplied
    tag lists, so it carries both defenses:

    1. Strip-and-re-stamp `contact:*`. Caller cannot address a delta to
       anyone but themselves.
    2. Reserved-tag scan. Authority-bearing tags must pass their gate
       (see docs/reserved-tags-spec.md + api/reserved_tags.py).

    If the body carries `image_b64` or `image_path`, the delta is written
    as an image-modality delta via `delta_client.upload_media` — content
    becomes the caption. Same tag gates apply.
    """
    contact = getattr(request.state, "contact", None)
    caller_slug = (contact or {}).get("slug")

    # (1) Strip caller-supplied contact:* tags; re-stamp with the
    # authenticated caller's slug if we have one. Internal-tag callers
    # never use this endpoint, so unknown-caller writes don't carry a
    # contact tag at all — that's fine; the reservation scan will catch
    # any authority-bearing write that came in unauthenticated.
    caller_tags = reserved_tags.strip_contact_tags(list(body.get("tags") or []))
    if caller_slug:
        caller_tags.append(f"contact:{caller_slug}")
    body = {**body, "tags": caller_tags}

    # (2) Reservation gate.
    result = await reserved_tags.evaluate(caller_tags, contact)
    if not result.ok:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "reserved_tag",
                "tag": result.tag,
                "gate": result.gate,
                "detail": result.hint or "",
            },
        )

    # (3) Image branch — if the caller attached an image, hand off to
    # upload_media which writes a multimodal delta (modality=image, with
    # the image stored on disk and content kept as the caption).
    image_path = body.get("image_path")
    image_b64 = body.get("image_b64")
    if image_path or image_b64:
        if image_path:
            try:
                file_bytes = await asyncio.to_thread(Path(image_path).read_bytes)
            except (FileNotFoundError, PermissionError, OSError) as e:
                raise HTTPException(status_code=400, detail=f"image_path unreadable: {e}") from e
            filename = Path(image_path).name or "upload.bin"
        else:
            try:
                file_bytes = base64.b64decode(image_b64, validate=True)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"image_b64 decode failed: {e}") from e
            filename = "upload.bin"
        return await delta_client.upload_media(
            file_bytes=file_bytes,
            filename=filename,
            content=body.get("content", "") or "",
            tags=caller_tags,
            source=body.get("source") or "api",
            expires_at=body.get("expires_at"),
        )

    c = await delta_client._get()
    r = await c.post("/deltas", json=body)
    r.raise_for_status()
    return r.json()


@router.get("/v1/deltas")
async def proxy_query_deltas(
    limit: int = 50,
    tags_include: str | None = None,
    source: str | None = None,
    time_start: str | None = None,
):
    c = await delta_client._get()
    params: dict = {"limit": limit}
    if tags_include:
        params["tags_include"] = tags_include
    if source:
        params["source"] = source
    if time_start:
        params["time_start"] = time_start
    r = await c.get("/deltas", params=params)
    r.raise_for_status()
    return r.json()


@router.get("/v1/deltas/{delta_id}")
async def proxy_get_delta(delta_id: str):
    c = await delta_client._get()
    r = await c.get(f"/deltas/{delta_id}")
    if r.status_code == 404:
        raise HTTPException(404, "Delta not found")
    r.raise_for_status()
    return r.json()


@router.post("/v1/plan")
async def proxy_plan(request: dict):
    c = await delta_client._get()
    r = await c.post("/plan", json=request)
    r.raise_for_status()
    return r.json()


@router.get("/v1/tags")
async def proxy_tags():
    c = await delta_client._get()
    r = await c.get("/tags")
    r.raise_for_status()
    return r.json()


@router.get("/v1/stats")
async def proxy_stats():
    return await delta_client.stats()
