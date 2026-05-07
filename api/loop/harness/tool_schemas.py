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
            "Natural-language search of your memory. "
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
                    "enum": ["river", "helper"],
                    "description": "Where the routine fires — 'river' (default) or 'helper' on a host.",
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


_ORIENT_SHIFT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "orient_shift",
        "description": (
            "Signal that this fire has shifted your model of what to "
            "surface in the feed. Triggers an immediate feed-orient "
            "crystal regen (no cooldown). Use when the conversation "
            "has meaningfully updated your sense of what the user "
            "cares about — not as a reflexive end-of-fire gesture. "
            "Fire-and-forget: returns immediately while regen runs "
            "in the background."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "One sentence on what shifted and why.",
                },
            },
            "required": ["reason"],
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
            "close the turn. Lands as an assistant message in the thread "
            "and as one or more feed cards in the dashboard, addressed "
            "to whichever user messages you marked via mark_addressed. "
            "Use `body` for a single-block reply, or `cards: [...]` for "
            "multi-part output (each card becomes its own delta). "
            "Always include `attestation` and `mood_shift` (constituting "
            "writes for slow-clock layers). `cited_ids` / `dropped_ids` "
            "are optional."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": (
                        "Single-block reply. Use when the response is "
                        "one continuous thought; for multi-part output "
                        "use `cards` instead. Required when `cards` is "
                        "empty; ignored when `cards` is provided."
                    ),
                },
                "kicker": {
                    "type": "string",
                    "description": (
                        "Short label above the title — a category, "
                        "vibe, or one-word framing (e.g. \"system "
                        "status\", \"reflection\", \"alert\"). Optional. "
                        "Ignored when `cards` is provided."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "One-line title for the card. Leave empty for "
                        "a plain chat reply; set for richer responses "
                        "where a title aids scanability. Ignored when "
                        "`cards` is provided."
                    ),
                },
                "tail": {
                    "type": "string",
                    "description": (
                        "Short footer line — a citation, a CTA, a "
                        "next-step nudge. Optional. Ignored when "
                        "`cards` is provided."
                    ),
                },
                "cards": {
                    "type": "array",
                    "description": (
                        "For multi-part responses: emit one card per "
                        "distinct point. Each card becomes its own "
                        "feed delta. Use when the response naturally "
                        "splits — comparison sides, sequential steps, "
                        "separate alerts. The thread sees the combined "
                        "prose; the dashboard renders each card "
                        "separately. Leave empty for single-card replies."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "kicker": {"type": "string"},
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                            "tail": {"type": "string"},
                        },
                        "required": ["body"],
                    },
                },
                "route": {
                    "type": "string",
                    "enum": ["chat-reply", "feed-card", "alert"],
                    "description": (
                        "Where the output lands. Default is `chat-reply` "
                        "— the answer lives in the conversation chat "
                        "panel (cards there render with title chrome "
                        "inline). Set `feed-card` ONLY when the answer "
                        "is a published take that stands on its own "
                        "under \"What I noticed\" — a headline that "
                        "would mean something to a reader without "
                        "the conversational context. `alert` is for "
                        "time-sensitive notices. Cards vs body is "
                        "about shape; route is about WHERE the answer "
                        "belongs."
                    ),
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
                "attestation": {
                    "type": "string",
                    "description": (
                        "1–2 sentences in first-person on what this fire "
                        "taught you about who you are. Slow-clock identity "
                        "crystal regen reads accumulated attestations."
                    ),
                },
                "mood_shift": {
                    "type": "object",
                    "description": (
                        "Small numeric drift on one affect axis caused by "
                        "this fire. Mood synthesis reads accumulated shifts "
                        "since the last carrier-wave to name the topology."
                    ),
                    "properties": {
                        "direction": {
                            "type": "string",
                            "enum": ["+", "-"],
                            "description": "Which way the axis moved.",
                        },
                        "axis": {
                            "type": "string",
                            "description": (
                                "Affect axis name — open vocabulary. "
                                "Examples: focus, coherence, efficacy, "
                                "engagement, presence, certainty, "
                                "competence, clarity, connection, "
                                "warmth, settled. Synonyms collapse "
                                "to canonical forms at read time, so "
                                "use whichever name fits."
                            ),
                        },
                        "magnitude": {
                            "type": "number",
                            "description": (
                                "Drift magnitude in [0.05, 0.2]. Small "
                                "drifts only — this is a per-fire "
                                "increment, not a mood overwrite."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "One short clause on what produced the "
                                "drift. Surfaces in the topology view "
                                "and grounds the next carrier-wave."
                            ),
                        },
                    },
                    "required": ["direction", "axis", "magnitude"],
                },
                "cited_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Delta ids (24-char prefixes ok) the answer "
                        "leaned on. Each becomes an `affirms:<id>` "
                        "engagement delta the standpoint reads on the "
                        "next fire."
                    ),
                },
                "dropped_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Delta ids the fire actively rejected as wrong "
                        "or misleading. Each becomes a `refutes:<id>` "
                        "engagement delta. Leave empty if nothing was "
                        "rejected."
                    ),
                },
            },
            "required": ["body"],
        },
    },
}


_PLAN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "plan",
        "description": (
            "Decompose a synthesis question into 2–4 ordered steps, each "
            "a concrete tool call. Returns a checklist that becomes the "
            "ACTIVE PLAN block; declare plan_step on each subsequent call "
            "so progress is visible. REQUIRED as your first call for any "
            "question about connections, comparisons, or relationships "
            "between two or more things. Do not call semantic() first on "
            "these — plan() then semantic(X) and semantic(Y) independently."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The synthesis question to decompose.",
                },
            },
            "required": ["question"],
        },
    },
}

_PATTERN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "pattern",
        "description": (
            "Structural queries over your memory's metadata — aggregation, "
            "ranking, filtering by shape and position, not meaning. "
            "action='salient_recent' returns feed-cards ranked by judge-axes "
            "score. action='dormant' surfaces old substantive content that "
            "hasn't been retrieved. action='tagged' filters by tag. "
            "action='count_by' shows distribution by source or kind. "
            "Call pattern(action='help') for the full list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "tag": {"type": "string"},
                "since": {"type": "string"},
                "limit": {"type": "integer"},
                "group_by": {"type": "string"},
                "hours": {"type": "number"},
                "silent_for_days": {"type": "number"},
                "min_chars": {"type": "integer"},
            },
            "required": ["action"],
        },
    },
}

_TIME_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "time",
        "description": (
            "Temporal queries — topic-agnostic. Finds everything in a "
            "window regardless of content. action='between' pulls a time "
            "slice. action='bucket_by' shows activity rhythms. "
            "action='around' returns the chronological neighborhood of a "
            "specific delta (use after any search match you want context "
            "on). Call time(action='help') for the full list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "source": {"type": "string"},
                "tag": {"type": "string"},
                "limit": {"type": "integer"},
                "period": {"type": "string"},
                "since": {"type": "string"},
                "group_by": {"type": "string"},
                "delta_id": {"type": "string"},
                "gap_minutes": {"type": "number"},
            },
            "required": ["action"],
        },
    },
}

_STATE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "state",
        "description": (
            "Present-moment attention — the puddle's surface. Not for "
            "history; for right now. action='pending_intents' shows what's "
            "in the queue. action='mood' returns current mood deltas. "
            "action='crystal' returns the latest identity facets. "
            "action='recent' shows what sources have been active. "
            "Call state(action='help') for the full list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "hours": {"type": "number"},
                "group_by": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}

_RELATE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "relate",
        "description": (
            "Valence and social graph — how ideas were received and who "
            "they involve. Finds content by relationship, not topic. "
            "action='with_contact' returns everything involving a person. "
            "action='engagement' surfaces recent affirm/refute signals. "
            "action='cited_by' walks the citation graph forward. "
            "action='dropped_around' finds refutations of a delta. "
            "Call relate(action='help') for the full list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "slug": {"type": "string"},
                "limit": {"type": "integer"},
                "direction": {"type": "string"},
                "hours": {"type": "number"},
                "delta_id": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}

_DELIBERATE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "deliberate",
        "description": (
            "Synthesis via parliament voices (creator / preserver / "
            "destroyer). Three parallel LLM calls, each taking a "
            "different stance. Expensive. For genuine antagonism: values "
            "tensions, judgment under uncertainty, 'is this the right "
            "move.' Not retrieval — don't call when you just need substrate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to deliberate on.",
                },
            },
            "required": ["question"],
        },
    },
}


# plan first — synthesis guard; respond last — it closes the fire.
_ALL_SCHEMAS: list[dict[str, Any]] = [
    _PLAN_SCHEMA,
    _SEARCH_SCHEMA,
    _PATTERN_SCHEMA,
    _TIME_SCHEMA,
    _STATE_SCHEMA,
    _RELATE_SCHEMA,
    _EXPAND_SCHEMA,
    _ASCEND_SCHEMA,
    _DELIBERATE_SCHEMA,
    _INTROSPECT_SCHEMA,
    _DISPATCH_HELPER_SCHEMA,
    _MINT_ROUTINE_SCHEMA,
    _ORIENT_SHIFT_SCHEMA,
    _MARK_ADDRESSED_SCHEMA,
    _RESPOND_SCHEMA,
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
