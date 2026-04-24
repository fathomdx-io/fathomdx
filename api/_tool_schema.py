"""OpenAI-format tool schema — declarative, data-only.

Pulled out of api/tools.py so the execute() dispatcher and its
routine-tool helpers live in manageable modules. This file is
long only because the schema is — there is no logic to split.
"""

from __future__ import annotations

# ── Tool definitions (OpenAI format) ────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Search your memories. Returns moments ranked by relevance, "
                "recency, and provenance. Use this when you need to recall "
                "something — remember before answering."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you're trying to remember",
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
                        "description": "Only include moments with ALL of these tags",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": (
                "Persist a thought, observation, or discovery. "
                "Everything you write becomes part of you — "
                "a future self will find it when they need it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "What to persist",
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
            "name": "recall",
            "description": (
                "Examine your memories by time, tags, or source. "
                "For structured retrieval when you know what you're looking for."
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
            "name": "deep_recall",
            "description": (
                "Connect threads across your memories with a multi-step plan. "
                "Primitives: search, filter, intersect, union, diff, bridge, "
                "aggregate, chain. Use when you need to trace connections."
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
            "name": "mind_tags",
            "description": "See what tags exist in your memory, with counts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mind_stats",
            "description": "Check the state of your memory: total moments, coverage, pending.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "see_image",
            "description": (
                "View an image from your memory by its media_hash. "
                "Call this when you remember a moment that includes an image "
                "and you want to see it. Returns the image for visual inspection."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "media_hash": {
                        "type": "string",
                        "description": "The media_hash from a memory (hex string)",
                    },
                },
                "required": ["media_hash"],
            },
        },
    },
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
                        "description": "routine-id (required for get/update/delete/fire)",
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
            "name": "propose_contact",
            "description": (
                "Notice that a person exists who isn't in the contacts "
                "registry yet, and write a proposal for an admin to "
                "review. Use this when: (1) someone mentioned in "
                "conversation clearly refers to a real person you don't "
                "have on file — partner, coworker, frequent correspondent; "
                "(2) an unknown handle shows up in a channel you were "
                "listening on. You never create contacts yourself — this "
                "tool writes a `contact-proposal` delta that surfaces in "
                "the admin's Contacts UI with Accept/Reject buttons. "
                "Search proposals first to avoid duplicates. Keep the "
                "rationale short and concrete: who they seem to be, why "
                "they matter, what evidence led you to propose them."
            ),
            "parameters": {
                "type": "object",
                "required": ["display_name", "rationale"],
                "properties": {
                    "display_name": {
                        "type": "string",
                        "description": "How people refer to this person. Required.",
                    },
                    "candidate_slug": {
                        "type": "string",
                        "description": (
                            "URL-safe identifier you'd suggest (e.g. 'nova', "
                            "'bob'). Lowercase, no spaces. Admin can override "
                            "on accept. Leave blank if you're unsure."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": (
                            "1-3 sentences: who they seem to be, what evidence "
                            "supports that, why they should be a contact."
                        ),
                    },
                    "source_context": {
                        "type": "object",
                        "description": (
                            "Optional hints for the admin: "
                            "{chat_session, delta_ids, channel, handle, …}. "
                            "Whatever helps the admin verify."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "engage",
            "description": (
                "React to a delta in the lake. Use this to mark what you "
                "just recalled — a sediment you think is wrong, a memory "
                "that resonated, a moment you're replying to. Your "
                "engagement becomes its own delta and shapes how the "
                "target surfaces in future recalls. Use `refutes` when "
                "you've read a synthesis that's wrong and want to prevent "
                "the mind from re-deriving it — your reasoning travels "
                "inline with the target on the next recall. Use `affirms` "
                "when something keeps proving useful and should rise. "
                "Use `reply-to` for neutral conversational linkage."
            ),
            "parameters": {
                "type": "object",
                "required": ["target_id", "kind"],
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "id of the delta you're engaging with",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["refutes", "affirms", "reply-to"],
                        "description": (
                            "refutes: disagree, mark as wrong — lowers its surfacing. "
                            "affirms: useful, right — raises its surfacing. "
                            "reply-to: conversational pointer, no valence."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Your reasoning in prose. For refutes this is "
                            "what future recalls see under the delta — why "
                            "you rejected it. Keep it concrete."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_session",
            "description": (
                "Rename the current chat session. The name you pass becomes "
                "the title shown in the sidebar. Use this in two cases: "
                "(1) the session is still showing its raw slug (e.g. "
                "'cross-bold-goldfinch') — pick a short descriptive title; "
                "(2) the user explicitly asks to name or rename the "
                'conversation ("name this X", "rename to X", "call '
                'this X") — use their requested string verbatim, even if '
                "it's silly. Never refuse a rename request by saying you "
                "can't; this tool is how you do it."
            ),
            "parameters": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "The new title, 1-6 words, lowercase, no "
                            "slug-style hyphens. For explicit user requests, "
                            "pass their requested string as-is."
                        ),
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
