"""OpenAI-format tool schema for the chat-surface tool list.

The canonical registry for lake-dispatched tools (remember, write,
recall, deep_recall, see_image, mind_stats, mind_tags, propose_contact,
engage, send_message, dispatch_helper, mint_routine) lives in
`api/routes/lake.py` as `LAKE_TOOLS`. We convert each chat-scoped
entry to OpenAI function-calling shape here with `to_openai_schema()`.

Crystal regen (`_generate_crystal_candidate` in `server.py`) is the
only remaining caller of this `TOOLS` list. The user-facing chat path
(`/v1/chat/completions`) runs entirely through the threaded harness,
which carries its own tool schemas in
`api/loop/harness/tool_schemas.py`.
"""

from __future__ import annotations

from .routes.lake import LAKE_TOOLS

__all__ = ["TOOLS", "to_openai_schema"]


def to_openai_schema(entry: dict) -> dict:
    """Convert a LAKE_TOOLS entry to an OpenAI function-calling tool.

    Strips registry-internal metadata (endpoint, request_map, scope,
    surfaces, response_kind) — those describe HTTP dispatch and
    client-side rendering, not the model-facing interface.
    """
    return {
        "type": "function",
        "function": {
            "name": entry["name"],
            "description": entry["description"],
            "parameters": entry.get("parameters") or {"type": "object", "properties": {}},
        },
    }


TOOLS: list[dict] = [
    to_openai_schema(t) for t in LAKE_TOOLS if "chat" in (t.get("surfaces") or [])
]
