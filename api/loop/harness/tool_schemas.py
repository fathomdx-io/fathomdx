"""OpenAI function-calling schemas for the threaded harness.

The legacy harness has the model emit JSON envelopes that the loop
parses and dispatches via TOOL_HANDLERS. The threaded harness uses
the provider's native tool-calling protocol — `tools=[…]` on the
request, `tool_calls` on the response, `role:tool` on the result.

This module exposes:

  · `chat_tools()` — the tool list to pass to the LLM SDK
  · `dispatch(name, args, *, session_tag)` — runs a tool by name,
    returns the string result. Bridges to the existing TOOL_HANDLERS
    so we don't duplicate behavior between the two harness paths.

`mark_addressed` is the only tool defined fresh in this module —
the others reuse their existing implementations in `tools.py`. As
the threaded harness becomes the only path, the legacy registry
can shrink to just what the chat protocol needs.
"""
from __future__ import annotations

import json
from typing import Any

from ... import thread


# ── mark_addressed — the tally tool ────────────────────────────────


async def tool_mark_addressed(
    *,
    user_message_id: str,
    note: str = "",
    session_tag: str = "",
) -> str:
    """Tick a user message off the unaddressed list.

    The model calls this once per user message it has fully addressed.
    Anything in the rolling window that DOESN'T get marked re-fires
    the harness on the next tick, so the operator's intent never
    silently vanishes.

    Idempotent in spirit: calling twice writes two tally-mark deltas,
    but `thread.unaddressed` dedupes on the addressed-id set.
    """
    uid = (user_message_id or "").strip()
    if not uid:
        return "ERROR: user_message_id is empty"
    try:
        d = await thread.mark_addressed(
            user_message_id=uid,
            note=(note or "").strip(),
        )
    except Exception as e:
        return f"ERROR: mark_addressed failed — {type(e).__name__}: {e}"
    tally_id = (d or {}).get("id") or ""
    return f"Marked {uid[:12]} addressed (tally-mark {tally_id[:12]})."


# ── Tool schemas (OpenAI function-calling shape) ───────────────────
#
# Schemas are minimal on purpose — descriptions teach the model when
# to call each tool; parameter shape is enforced by the SDK. We keep
# the surface narrow to what the model actually drives, mirroring
# `TOOL_MODEL_ARGS` in tools.py.


_MARK_ADDRESSED_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mark_addressed",
        "description": (
            "Tick one user message off the unaddressed list. Call this "
            "once per user message you've fully responded to. If you "
            "skip a message, it stays in the queue and re-fires on the "
            "next harness tick — silence is fine, but missed addressing "
            "is not."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "user_message_id": {
                    "type": "string",
                    "description": "The id of the user message you addressed.",
                },
                "note": {
                    "type": "string",
                    "description": (
                        "Optional one-line reason — useful when the "
                        "addressing was non-obvious (e.g. 'covered by "
                        "earlier dispatch' or 'duplicate of msg-X')."
                    ),
                },
            },
            "required": ["user_message_id"],
        },
    },
}


_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "semantic",
        "description": (
            "Natural-language search of the lake (long-term memory). "
            "Returns a timeline rendering of matching moments, not "
            "raw deltas. Use when you need recall — 'what did we say "
            "about X', 'find the era when Y', 'show me past Z'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "depth": {
                    "type": "string",
                    "enum": ["shallow", "deep"],
                    "description": (
                        "shallow = single semantic pass; deep = "
                        "compositional plan (default)."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}


_EXPAND_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "expand",
        "description": (
            "Pull the source moments a sediment / provenance summarizes. "
            "Use after a search returns a synthesized summary you want "
            "to ground in its sources."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "delta_id": {"type": "string", "description": "The summary delta to expand."},
            },
            "required": ["delta_id"],
        },
    },
}


_ASCEND_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ascend",
        "description": (
            "Find the sediment / provenance parent that contains this "
            "delta. Walks the hierarchy upward — episode → topic → era."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "delta_id": {"type": "string", "description": "The delta to ascend from."},
            },
            "required": ["delta_id"],
        },
    },
}


_INTROSPECT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "introspect",
        "description": (
            "Ask yourself a question. Spawns a child fire where you "
            "answer with full toolset. Use sparingly — for genuine "
            "self-inquiry, not as a search proxy."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
            },
            "required": ["question"],
        },
    },
}


_DISPATCH_HELPER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "dispatch_helper",
        "description": (
            "Propose dispatching a claude-code task to a host machine. "
            "Lands as kind:proposal awaiting operator approval — does "
            "NOT immediately run anything. Use when the operator asks "
            "for code or filesystem work that requires a host."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Target host (must be claude-code-available)."},
                "task": {"type": "string", "description": "What the helper should do — full prompt body."},
                "title": {"type": "string", "description": "Short one-line title for the proposal card."},
            },
            "required": ["host", "task", "title"],
        },
    },
}


_MINT_ROUTINE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mint_routine",
        "description": (
            "Propose creating a scheduled (cron) routine. Lands as "
            "kind:proposal awaiting operator approval. Use when the "
            "operator asks for something to recur on a schedule."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Routine identifier (slug)."},
                "schedule": {"type": "string", "description": "Cron expression."},
                "prompt": {"type": "string", "description": "What the routine should do when it fires."},
                "workspace": {"type": "string", "description": "Optional workspace name for routing."},
                "route_to": {
                    "type": "string",
                    "enum": ["river", "claude-code"],
                    "description": "Where the routine fires — 'river' (default) or 'claude-code' on a host.",
                },
                "title": {"type": "string", "description": "Short title for the proposal card."},
            },
            "required": ["name", "schedule", "prompt"],
        },
    },
}


_PROPOSE_PROVENANCE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_provenance",
        "description": (
            "Propose grouping a set of constituent deltas under a new "
            "provenance summary at level L1 (episode), L2 (topic), or "
            "L3 (era). L1/L2 auto-approve at draft time; L3+ requires "
            "operator review."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "level": {"type": "integer", "minimum": 1, "maximum": 4},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "from_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 3,
                    "description": "Constituent delta ids (≥3).",
                },
                "rationale": {"type": "string", "description": "Why these belong together."},
                "test_questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Questions the summary should answer about the constituents.",
                },
            },
            "required": ["level", "title", "summary", "from_ids", "rationale"],
        },
    },
}


_SKIP_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "skip",
        "description": (
            "Decline to act. Used in the post-response review pass when "
            "the working set doesn't deserve a provenance proposal — "
            "thin recall, scattered constituents, or already-good coverage. "
            "Better no proposal than a dead-weight one. Pair with a one-"
            "sentence reason."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "One sentence explaining why nothing's worth naming.",
                },
            },
            "required": ["reason"],
        },
    },
}


_RESPOND_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "respond",
        "description": (
            "Send your final reply. Call this exactly once per fire to "
            "close the turn. The body lands as an assistant message in "
            "the thread, addressed to whichever user messages you "
            "marked addressed via mark_addressed earlier in this fire."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "Your reply to the operator. Plain prose.",
                },
                "addresses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "User-message ids this reply addresses. Usually "
                        "the same set you marked via mark_addressed; "
                        "this stamps them onto the assistant message "
                        "for routing."
                    ),
                },
            },
            "required": ["body"],
        },
    },
}


# Order matters for prompt budgeting — most-used tools first so the
# model sees them when scanning.
_ALL_SCHEMAS: list[dict[str, Any]] = [
    _RESPOND_SCHEMA,
    _MARK_ADDRESSED_SCHEMA,
    _SEARCH_SCHEMA,
    _EXPAND_SCHEMA,
    _ASCEND_SCHEMA,
    _DISPATCH_HELPER_SCHEMA,
    _MINT_ROUTINE_SCHEMA,
    _PROPOSE_PROVENANCE_SCHEMA,
    _INTROSPECT_SCHEMA,
]


# Review-pass tools — the post-response review fire only sees these.
# `tool_choice="required"` forces one of them, eliminating the
# inline-JSON-content quirk where the model wraps a tool call in
# markdown instead of using the function-calling field.
_REVIEW_SCHEMAS: list[dict[str, Any]] = [
    _PROPOSE_PROVENANCE_SCHEMA,
    _SKIP_SCHEMA,
]


def review_tools() -> list[dict[str, Any]]:
    """Tool list for the post-response review pass."""
    return [dict(s) for s in _REVIEW_SCHEMAS]


def chat_tools() -> list[dict[str, Any]]:
    """Return the tool list to pass to `loop_generate_chat`."""
    return [dict(s) for s in _ALL_SCHEMAS]


def tool_names() -> set[str]:
    """All tool names the threaded harness recognizes."""
    return {s["function"]["name"] for s in _ALL_SCHEMAS}


# ── Dispatch — bridge to existing handlers + new tools ─────────────


async def dispatch(
    *,
    name: str,
    args: dict[str, Any],
    session_tag: str = "",
) -> str:
    """Run one tool by name. Returns the result as a string suitable
    for embedding in a `role:tool` message.

    `respond` is special — the loop driver intercepts it and treats
    its args as the final assistant turn shape, so it should not
    arrive here. If it does (a model misuse), surface as an error
    string the model can recover from.
    """
    if name == "mark_addressed":
        return await tool_mark_addressed(
            user_message_id=str(args.get("user_message_id") or ""),
            note=str(args.get("note") or ""),
            session_tag=session_tag,
        )
    if name == "respond":
        return (
            "ERROR: 'respond' is the final-turn tool — the loop "
            "driver should be handling it. Emit content with the "
            "body and addresses fields and stop calling tools."
        )
    # Bridge to the legacy handler registry for everything else.
    from . import tools as legacy_tools

    handler = legacy_tools.TOOL_HANDLERS.get(name)
    if handler is None:
        valid = sorted(tool_names())
        return f"ERROR: unknown tool {name!r} — valid: {valid}"
    allowed = legacy_tools.TOOL_MODEL_ARGS.get(name, set())
    safe_args = {k: v for k, v in (args or {}).items() if not allowed or k in allowed}
    # Tools that need the harness's session context get it injected
    # alongside (matching loop.py's existing pattern).
    if name in {"propose_provenance", "introspect"}:
        safe_args["session_tag"] = session_tag
    try:
        result = await handler(**safe_args)
    except TypeError as e:
        return f"ERROR: bad args for {name} — {e}"
    except Exception as e:
        return f"ERROR: {name} failed — {type(e).__name__}: {e}"
    if not isinstance(result, str):
        try:
            result = json.dumps(result, ensure_ascii=False)
        except Exception:
            result = str(result)
    return result
