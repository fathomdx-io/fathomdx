"""Delta lake operations as OpenAI function-calling tools."""
from __future__ import annotations

import base64
import json

from . import delta_client

# ── Tool definitions (OpenAI format) ────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "delta_search",
            "description": (
                "Semantic search across the delta lake. Returns fragments ranked "
                "by meaning-similarity, recency, and provenance. Use this when the "
                "user asks about something — search before answering."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20, max 50)",
                        "default": 20,
                    },
                    "radii": {
                        "type": "object",
                        "description": "Dimension weights for ranking",
                        "properties": {
                            "temporal": {"type": "number", "default": 1.0},
                            "semantic": {"type": "number", "default": 1.0},
                            "provenance": {"type": "number", "default": 1.0},
                        },
                    },
                    "tags_include": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Only include deltas with ALL of these tags",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delta_write",
            "description": (
                "Write a new delta to the lake. Use for observations, decisions, "
                "discoveries — anything a future search should find."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The text content of the delta",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for categorization (2-4 recommended)",
                    },
                    "source": {
                        "type": "string",
                        "description": "Provenance label (default: consumer-api)",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delta_query",
            "description": (
                "Filter deltas by time, tags, or source. For structured lookups "
                "when you know what you're looking for — not semantic search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tags_include": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "source": {"type": "string"},
                    "time_start": {
                        "type": "string",
                        "description": "ISO-8601 timestamp",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delta_plan",
            "description": (
                "Execute a compositional query plan with multiple steps. "
                "Primitives: search, filter, intersect, union, diff, bridge, "
                "aggregate, chain. Use for complex multi-step analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Ordered list of plan steps. Each has 'id' (str) and "
                            "exactly one action key (search, filter, intersect, "
                            "union, diff, bridge, aggregate, chain) plus optional "
                            "radii, tags_include, limit, group_by, metric."
                        ),
                    },
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delta_tags",
            "description": "List all tags in the lake with their counts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delta_stats",
            "description": "Get lake statistics: total deltas, embedded count, pending.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delta_view_image",
            "description": (
                "View an image from the delta lake by its media_hash. "
                "Call this when search results include a delta with a media_hash "
                "and you want to see what the image contains. Returns the image "
                "for visual inspection."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "media_hash": {
                        "type": "string",
                        "description": "The media_hash from a delta (hex string)",
                    },
                },
                "required": ["media_hash"],
            },
        },
    },
]


# ── Tool execution ──────────────────────────────

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


async def execute(name: str, arguments: dict) -> str:
    """Execute a tool call, return result as JSON string."""
    try:
        if name == "delta_search":
            raw = await delta_client.search(
                query=arguments["query"],
                limit=arguments.get("limit", 20),
                radii=arguments.get("radii"),
                tags_include=arguments.get("tags_include"),
            )
            return json.dumps(_slim_search_results(raw))

        if name == "delta_write":
            result = await delta_client.write(
                content=arguments["content"],
                tags=arguments.get("tags", []),
                source=arguments.get("source", "consumer-api"),
            )
            return json.dumps(result)

        if name == "delta_query":
            raw = await delta_client.query(
                limit=arguments.get("limit", 50),
                tags_include=arguments.get("tags_include"),
                source=arguments.get("source"),
                time_start=arguments.get("time_start"),
            )
            return json.dumps(_slim_query_results(raw))

        if name == "delta_plan":
            result = await delta_client.plan(arguments["steps"])
            return json.dumps(result)

        if name == "delta_tags":
            result = await delta_client.tags()
            return json.dumps(result)

        if name == "delta_stats":
            result = await delta_client.stats()
            return json.dumps(result)

        if name == "delta_view_image":
            return await _fetch_image_as_tool_result(arguments.get("media_hash", ""))

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
