"""Memory operations as function-calling tools."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from . import delta_client
from . import messages as messages_mod
from ._engagement import build_engagement_payload
from ._tags import tag_suffix
from ._tool_schema import TOOLS

__all__ = ["TOOLS", "execute", "heartbeat_age_seconds", "heartbeat_is_fresh"]


# A heartbeat is considered "fresh" (agent connected) if it was emitted
# within this window. Heartbeats fire every ~60s, so 90s tolerates a
# single missed beat without flipping the UI to disconnected. Heartbeat
# deltas themselves live for 24h so the dashboard can still show a
# disconnected card after the connected window elapses.
HEARTBEAT_STALE_SECONDS = 90


def heartbeat_age_seconds(delta: dict) -> float | None:
    """Seconds since the given heartbeat delta was emitted, or None if unparseable."""
    ts = delta.get("timestamp", "")
    if not ts:
        return None
    try:
        hb = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    return (datetime.now(UTC) - hb).total_seconds()


def heartbeat_is_fresh(delta: dict) -> bool:
    age = heartbeat_age_seconds(delta)
    return age is not None and age < HEARTBEAT_STALE_SECONDS


# ── Tool execution ──────────────────────────────


async def _session_contact_slug(session_id: str) -> str | None:
    """Return the contact slug associated with a chat session, or None.

    Reads the contact: tag off any participant:user delta in the
    session — every chat turn is stamped with one. Used by send_message
    to default `to` to the session's human when the LLM omits it.
    """
    try:
        results = await delta_client.query(
            tags_include=[f"chat:{session_id}", "participant:user"],
            limit=1,
        )
    except Exception:
        return None
    for d in results:
        slug = tag_suffix(d.get("tags") or [], "contact:")
        if slug:
            return slug
    return None


def _slim_search_results(raw: dict) -> dict:
    """Strip embeddings, cap content length for context window."""
    hits = raw.get("results", [])
    slim = []
    for h in hits:
        d = h.get("delta", {})
        entry = {
            "id": d.get("id"),
            "content": d.get("content", "")[:1500],
            "tags": d.get("tags", []),
            "source": d.get("source"),
            "timestamp": d.get("timestamp"),
            "distance": round(h.get("distance", 0), 3),
        }
        if d.get("media_hash"):
            entry["media_hash"] = d["media_hash"]
        slim.append(entry)
    return {"count": len(slim), "results": slim}


def _slim_recall_for_tool(result: dict) -> dict:
    """Compact recall result for the chat-LLM tool channel.

    Returns the rendered timeline as `as_prompt` (the load-bearing
    context for the model to read), the structured `timelines` for any
    consumer that wants to walk the strips, and `total_count` /
    `media_hashes`. Drops `plan` / `tree` / `deltas_by_step` to keep
    the JSON payload small — the prose carries the meaning.
    """
    return {
        "as_prompt": result.get("as_prompt", ""),
        "timelines": result.get("timelines", []),
        "total_count": result.get("total_count", 0),
        "media_hashes": result.get("media_hashes", []),
        "thinking_prose": result.get("thinking_prose"),
        "thinking_id": result.get("thinking_id"),
    }


def _slim_query_results(raw: list) -> dict:
    """Same slimming for query results."""
    slim = []
    for d in raw:
        entry = {
            "id": d.get("id"),
            "content": d.get("content", "")[:1500],
            "tags": d.get("tags", []),
            "source": d.get("source"),
            "timestamp": d.get("timestamp"),
        }
        if d.get("media_hash"):
            entry["media_hash"] = d["media_hash"]
        slim.append(entry)
    return {"count": len(slim), "results": slim}


async def execute(name: str, arguments: dict, session_id: str | None = None) -> str:
    """Execute a tool call, return result as JSON string.

    `session_id` is injected from the API — the caller knows the current
    chat session and passes it in so tools that need it (route_to_agent)
    don't have to ask the model to pass it back as a parameter. The model
    wouldn't know anyway, and asking the user is always wrong.
    """
    try:
        if name == "remember":
            # Route through the canonical NL search so chat-LLM tool calls
            # see the same shape as MCP / CLI: timeline strips with
            # ambient context and anchors marked, instead of orphan delta
            # fragments. radii / tags_include are not threaded through —
            # they're shallow-only knobs and the canonical path is deep.
            from .search import search as nl_search

            result = await nl_search(
                text=arguments["query"],
                depth=arguments.get("depth", "deep"),
                limit=arguments.get("limit", 20),
                view="timeline",
            )
            return json.dumps(_slim_recall_for_tool(result))

        if name == "write":
            # image_b64 routes through upload_media so the model can attach
            # a picture to a write in one call. image_path is registry-only
            # (staging-volume path gated by the HTTP sandbox); chat ignores
            # it — the LLM should reach for image_b64 when it has pixels.
            image_b64 = arguments.get("image_b64")
            if image_b64:
                file_bytes = base64.b64decode(image_b64)
                result = await delta_client.upload_media(
                    file_bytes=file_bytes,
                    filename="upload.bin",
                    content=arguments["content"],
                    tags=arguments.get("tags", []),
                    source=arguments.get("source", "fathom-engagement"),
                )
            else:
                result = await delta_client.write(
                    content=arguments["content"],
                    tags=arguments.get("tags", []),
                    source=arguments.get("source", "fathom-engagement"),
                )
            return json.dumps(result)

        if name == "recall":
            # LAKE_TOOLS exposes the model-facing param as `tags`;
            # delta_client.query takes it as `tags_include`. The registry's
            # request_map handles that translation for HTTP callers (MCP);
            # in-process callers (chat) translate here.
            raw = await delta_client.query(
                limit=arguments.get("limit", 50),
                tags_include=arguments.get("tags"),
                source=arguments.get("source"),
                time_start=arguments.get("time_start"),
            )
            return json.dumps(_slim_query_results(raw))

        if name == "deep_recall":
            result = await delta_client.plan(arguments["steps"])
            return json.dumps(result)

        if name == "mind_tags":
            result = await delta_client.tags()
            return json.dumps(result)

        if name == "mind_stats":
            result = await delta_client.stats()
            return json.dumps(result)

        if name == "see_image":
            return await _fetch_image_as_tool_result(arguments.get("media_hash", ""))

        if name == "propose_contact":
            from . import contacts as contacts_mod

            written = await contacts_mod.propose(
                candidate_slug=(arguments.get("candidate_slug") or "").strip() or None,
                display_name=arguments["display_name"],
                rationale=arguments["rationale"],
                source_context=arguments.get("source_context") or {},
                # In the chat tool path, Fathom writes the proposal as
                # Fathom (no contact: tag) — the admin just needs to
                # know it's a proposal, not who proposed it.
                proposer_slug=None,
            )
            return json.dumps(
                {
                    "ok": True,
                    "proposal_id": written.get("id"),
                    "candidate_slug": written.get("candidate_slug"),
                    "display_name": written.get("display_name"),
                    "note": (
                        "Proposal written. Admin will see it in Settings → "
                        "Contacts and can Accept (creates the contact) or "
                        "Reject (keeps the proposal as sediment)."
                    ),
                }
            )

        if name == "engage":
            kind = (arguments.get("kind") or "").lower()
            if kind not in ("refutes", "affirms", "reply-to"):
                return json.dumps({"error": f"unknown engagement kind: {kind!r}"})
            target_id = (arguments.get("target_id") or "").strip()
            if not target_id:
                return json.dumps({"error": "target_id required"})
            reason = (arguments.get("reason") or "").strip()
            content, media_hash = await build_engagement_payload(target_id, reason)
            written = await delta_client.write(
                content=content,
                tags=[f"{kind}:{target_id}"],
                source="fathom-engagement",
                media_hash=media_hash,
            )
            return json.dumps(
                {
                    "ok": True,
                    "id": written.get("id"),
                    "kind": kind,
                    "target_id": target_id,
                }
            )

        if name == "send_message":
            recipient = (arguments.get("to") or "").strip()
            if not recipient and session_id:
                # Default-to-requestor: read the session's contact: tag from
                # any user delta in the thread. This is the LLM-in-chat
                # path; the human in the session is the natural recipient
                # for "alert me" / "remind me" instructions.
                recipient = await _session_contact_slug(session_id) or ""
            if not recipient:
                return json.dumps(
                    {
                        "error": (
                            "no recipient — pass `to` with a contact slug, or "
                            "call this tool inside a chat session so the "
                            "requestor can be inferred"
                        ),
                    }
                )
            body = arguments.get("body") or ""
            try:
                result = await messages_mod.send_message(
                    recipient_slug=recipient,
                    body=body,
                    writer_slug="fathom",
                    session_slug=arguments.get("session") or None,
                )
            except ValueError as e:
                return json.dumps({"error": str(e)})
            return json.dumps(result)

        return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# Sentinel prefix for multimodal image results — the tool loop
# in server.py detects this and converts to a content block.
IMAGE_RESULT_PREFIX = "__IMAGE__:"


async def _fetch_image_as_tool_result(media_hash: str) -> str:
    """Fetch image from delta store, return as a sentinel string.

    The tool loop in server.py detects the IMAGE_RESULT_PREFIX and
    converts this into a multimodal content block (image_url with
    base64 data URI) so the LLM actually sees the pixels.
    """
    if not media_hash:
        return json.dumps({"error": "No media_hash provided"})
    try:
        c = await delta_client._get()
        r = await c.get(f"/media/{media_hash}", timeout=15)
        r.raise_for_status()
        img_bytes = r.content
        b64 = base64.b64encode(img_bytes).decode("ascii")
        # Return sentinel so the tool loop can build a multimodal message
        return f"{IMAGE_RESULT_PREFIX}data:image/webp;base64,{b64}"
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch image: {e}"})
