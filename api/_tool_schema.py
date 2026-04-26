"""OpenAI-format tool schema for chat.

Chat's LLM expects OpenAI function-calling shape. The canonical registry
for lake-dispatched tools (remember, write, recall, deep_recall,
see_image, mind_stats, mind_tags, propose_contact, engage) lives in
`api/routes/lake.py` as `LAKE_TOOLS`. We convert each chat-scoped entry
to OpenAI shape here with `to_openai_schema()`.

Chat-only tools (routines, rename_session, explain) have no lake HTTP
endpoint — their execution is inline in `api/tools.py`. They stay
defined here in `CHAT_ONLY_TOOLS` in OAI shape directly.

The final exported `TOOLS` list is the union, filtered to the chat
surface. One source of truth; chat and MCP/CLI no longer drift because
they consume from the same registry.
"""

from __future__ import annotations

from .routes.lake import LAKE_TOOLS

__all__ = ["CHAT_ONLY_TOOLS", "TOOLS", "to_openai_schema"]


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


# ── Chat-only tools (no lake endpoint, dispatched inline) ──────────

CHAT_ONLY_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "routines",
            "description": (
                "Manage scheduled routines — prompts that fire into a local "
                "claude session on a cron schedule. Everything goes through "
                "this one tool via the `action` field. "
                "Start with action='help' to see the routine spec, or "
                "action='list' to see existing ones. "
                "If no local agent is connected the mutation actions "
                "(create/update/delete/fire) will return installation "
                "instructions — tell the user to visit the main page of the "
                "app to set one up. "
                "For action='create': the default flow is PROPOSE, NOT COMMIT. "
                "Call create with whatever fields you've composed (name, "
                "schedule, prompt at minimum — id/workspace/host may be blank) "
                "and the tool returns {status:'needs_confirmation'} while "
                "simultaneously painting a review form in the user's chat. "
                "The user edits and saves that form — you do NOT re-prompt the "
                "user for the fields in prose; just say something short like "
                "'Here's the routine — review and save.' Pass confirm=true "
                "only when the user has explicitly told you to skip the review "
                "step (e.g. 'just make it', 'don't ask, create it'). "
                "Outside a chat session (session_id absent), the tool commits "
                "directly and returns the result."
            ),
            "parameters": {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "help",
                            "list",
                            "get",
                            "create",
                            "update",
                            "delete",
                            "fire",
                            "preview_schedule",
                        ],
                        "description": (
                            "help: spec reference and action catalogue. "
                            "list: all current routines. "
                            "get: single routine by id. "
                            "create: new routine (requires id, name; schedule/prompt strongly recommended). "
                            "update: modify fields by id. "
                            "delete: soft-delete (tombstone) by id. "
                            "fire: trigger a routine to run now. "
                            "preview_schedule: show next N fire times for a cron string."
                        ),
                    },
                    "id": {
                        "type": "string",
                        "description": (
                            "routine-id slug — required for get/update/delete/fire. "
                            "For create, derive it from `name`: lowercase, hyphen-"
                            "separated, e.g. name 'Menya Rui hours check' → id "
                            "'menya-rui-hours-check'. If you omit it, the server "
                            "will slugify the name as a fallback."
                        ),
                    },
                    "name": {"type": "string", "description": "human-readable label"},
                    "schedule": {
                        "type": "string",
                        "description": "5-field cron (e.g. '0 * * * *' for hourly, '*/5 * * * *' every 5 min)",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "what claude should do when this routine fires",
                    },
                    "permission_mode": {
                        "type": "string",
                        "enum": ["auto", "normal"],
                        "description": (
                            "auto: classifier auto-approves safe actions. "
                            "normal: claude prompts for each tool (user approves)."
                        ),
                    },
                    "workspace": {
                        "type": "string",
                        "description": "directory under ~/Dropbox/Work/ where the kitty session opens",
                    },
                    "host": {
                        "type": "string",
                        "description": (
                            "which machine runs this routine — must match a connected "
                            "agent's hostname (e.g. 'fedora'). Empty = fleet-wide (every "
                            "connected agent will execute the fire). When unsure, call "
                            "action='help' to see the list of connected machines, or leave "
                            "blank and the tool will ask."
                        ),
                    },
                    "enabled": {"type": "boolean", "description": "paused if false"},
                    "single_fire": {
                        "type": "boolean",
                        "description": "documented but not yet honored by scheduler",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "create-only bypass. Default behavior proposes the "
                            "routine in a chat form for the user to review. "
                            "Set true only when the user explicitly asked you "
                            "to skip the review step."
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "description": "for preview_schedule: number of upcoming fires to return (default 5)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain",
            "description": (
                "Explain a part of the Fathom dashboard to the user. Call this "
                "whenever the user asks what something is, how it works, or how "
                "to set it up — covers sources, feed, stats, and agent. The tool "
                "returns a spec-style description blended with the user's live "
                "state (e.g. how many sources they have configured right now), "
                "so your answer can be concrete rather than generic. Prefer this "
                "over answering from general knowledge — the dashboard is "
                "opinionated and the tool is authoritative."
            ),
            "parameters": {
                "type": "object",
                "required": ["topic"],
                "properties": {
                    "topic": {
                        "type": "string",
                        "enum": ["sources", "feed", "stats", "agent"],
                        "description": (
                            "sources: pollers that write deltas into the lake (RSS, "
                            "Mastodon, HN, custom). "
                            "feed: the 'What I noticed' surface on the dashboard — "
                            "synthesized stories from recent lake activity. "
                            "stats: the time-series dashboard showing deltas-in, "
                            "recall, mood pressure, drift. "
                            "agent: the local fathom-agent runtime — what it runs "
                            "(routines, passive senses) and how to install it."
                        ),
                    },
                },
            },
        },
    },
]


# ── Computed: chat-only + lake-surface chat tools ──────────────────

TOOLS: list[dict] = CHAT_ONLY_TOOLS + [
    to_openai_schema(t) for t in LAKE_TOOLS if "chat" in (t.get("surfaces") or [])
]
