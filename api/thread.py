"""The single global thread — Fathom's stream of awareness.

The thread is the substrate the harness fires off. Every surface
that produces a message Fathom should attend to (composer typing,
OpenAI client requests, routine fires, dispatched-task closures,
the witness's own outputs, tool call/result pairs) appends to one
thread. There is no per-surface threading — Fathom is an individual,
and the thread is its working memory.

Storage: lake-backed. Each message is a delta with `kind:thread-msg`
plus role / msg-kind / channel / correlation / contact / addresses
tags. The lake is durable, so the thread survives restarts without
any rehydrate dance.

Routing: every user-role message carries `channel:<x>` and
`correlation:<y>` so the assistant's reply (which addresses the user
message via `addresses:<id>` tags) can be dispatched back to the
right surface.

Tally: the activation rule is "any user-role message in the rolling
window with no covering `kind:tally-mark addresses:<id>` is
unaddressed." A new unaddressed message is what wakes the harness.
The model marks messages addressed via `mark_addressed` (a tool)
or implicitly via `addresses:` tags on its response.
"""

from __future__ import annotations

from typing import Any

from . import delta_client

# ── Tag constants ──────────────────────────────────────────────────

KIND_THREAD_MSG = "kind:thread-msg"
KIND_TALLY_MARK = "kind:tally-mark"

VALID_ROLES: frozenset[str] = frozenset({"user", "assistant", "tool", "system"})


def _t(prefix: str, value: str) -> str:
    return f"{prefix}:{value}"


# ── Append API ─────────────────────────────────────────────────────


async def append(
    *,
    role: str,
    msg_kind: str,
    content: str,
    channel: str = "",
    correlation: str = "",
    contact: str = "",
    addresses: list[str] | None = None,
    tool_call_id: str = "",
    tool_name: str = "",
    extra_tags: list[str] | None = None,
    source: str = "thread",
    timestamp: str | None = None,
    media_hash: str = "",
    expires_at: str | None = None,
) -> dict:
    """Append one message to the global thread.

    `role` is the OpenAI-shape role — what the LLM sees natively.
    `msg_kind` is the Fathom-level type (composer, openai-chat,
    routine-fire, claude-code-reply, chat-reply, helper-dispatch,
    tool-call, tool-result, …) used for routing and rendering.

    `channel + correlation` is the routing pair: the assistant's
    reply addressing this message will be dispatched back to that
    surface. `contact` is the operator/contact slug.

    `addresses` is for assistant messages — the user-message ids
    this turn answers. The lake delta will carry one
    `addresses:<id>` tag per entry.

    `tool_call_id` / `tool_name` are for OpenAI tool-use shape:
    role=assistant with tool_calls carries the call id(s); role=tool
    carries the matching call id and the tool name in its tags.

    Returns the lake delta dict (carries `id`, `timestamp`).
    """
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(VALID_ROLES)}, got {role!r}")
    if not msg_kind:
        raise ValueError("msg_kind required")

    tags = [KIND_THREAD_MSG, _t("role", role), _t("msg-kind", msg_kind)]
    if channel:
        tags.append(_t("channel", channel))
    if correlation:
        tags.append(_t("correlation", correlation))
    if contact:
        tags.append(_t("contact", contact))
    if tool_call_id:
        tags.append(_t("tool-call-id", tool_call_id))
    if tool_name:
        tags.append(_t("tool-name", tool_name))
    for addr in addresses or []:
        if addr:
            tag = _t("addresses", addr)
            if tag not in tags:
                tags.append(tag)
    for t in extra_tags or []:
        if t and t not in tags:
            tags.append(t)

    write_args: dict[str, Any] = {
        "content": content,
        "tags": tags,
        "source": source,
    }
    if timestamp:
        write_args["timestamp"] = timestamp
    if media_hash:
        write_args["media_hash"] = media_hash
    if expires_at:
        write_args["expires_at"] = expires_at
    return await delta_client.write(**write_args)


# ── Tally API ──────────────────────────────────────────────────────


async def mark_addressed(
    *,
    user_message_id: str,
    note: str = "",
    by: str = "harness",
) -> dict:
    """Record that a user message has been fully addressed.

    Idempotent in spirit (calling twice writes two marks, but
    `unaddressed` dedupes). Soft-fails — caller decides what to
    do on lake error.
    """
    if not user_message_id:
        raise ValueError("user_message_id required")
    return await delta_client.write(
        content=note,
        tags=[
            KIND_TALLY_MARK,
            _t("addresses", user_message_id),
            _t("marked-by", by),
        ],
        source="thread-tally",
    )


# ── Read API ───────────────────────────────────────────────────────

# Per-message overhead estimate (role/kind framing + JSON envelope
# overhead when the LLM client packs into chat-completions shape).
# Conservative — better to under-pack than blow the budget.
_PER_MESSAGE_TOKEN_OVERHEAD = 20


def _estimate_tokens(rows: list[dict]) -> int:
    """Rough token estimate. ~4 chars/token + per-message overhead."""
    total = 0
    for r in rows:
        content = r.get("content") or ""
        total += (len(content) + 3) // 4 + _PER_MESSAGE_TOKEN_OVERHEAD
    return total


async def tail(
    *,
    limit: int = 15,
    max_tokens: int | None = None,
) -> list[dict]:
    """Return the rolling-window tail of the thread, oldest-first.

    `limit` is the message-count cap. `max_tokens`, if provided,
    further trims from the head until the estimated tokens fits
    the budget. Returns at most `limit` rows; may return fewer if
    a single message exceeds the token cap.
    """
    rows = await delta_client.query(
        tags_include=[KIND_THREAD_MSG],
        limit=max(limit * 2, 30),
    )
    rows.sort(key=lambda d: d.get("timestamp") or "")
    rows = rows[-limit:]
    if max_tokens is not None:
        while len(rows) > 1 and _estimate_tokens(rows) > max_tokens:
            rows = rows[1:]
        if rows and _estimate_tokens(rows) > max_tokens:
            # Single oversized message — let it through; caller decides
            # whether to truncate content. Better than returning empty.
            pass
    return rows


def _role(d: dict) -> str:
    for t in d.get("tags") or []:
        if isinstance(t, str) and t.startswith("role:"):
            return t.split(":", 1)[1]
    return ""


async def unaddressed(rows: list[dict]) -> list[dict]:
    """Filter `rows` to user-role messages with no covering tally
    mark, oldest-first.

    The query for tally marks is bounded — pulls recent ones, builds
    an addressed-id set, filters. Cheap because tally marks are
    small and infrequent compared to feed-card / witness deltas.
    """
    user_msgs = [r for r in rows if _role(r) == "user"]
    if not user_msgs:
        return []
    marks = await delta_client.query(
        tags_include=[KIND_TALLY_MARK],
        limit=500,
    )
    addressed: set[str] = set()
    for m in marks:
        for t in m.get("tags") or []:
            if isinstance(t, str) and t.startswith("addresses:"):
                addressed.add(t.split(":", 1)[1])
    return [r for r in user_msgs if (r.get("id") or "") not in addressed]


async def build_window(
    *,
    max_messages: int = 15,
    max_tokens: int | None = None,
) -> dict:
    """Return the active rolling window plus its unaddressed list.

    `{"messages": [oldest, …, newest], "unaddressed": [oldest, …, newest]}`

    The harness uses `messages` to construct the LLM call's
    `messages` array; the dashboard's tally overlay reads
    `unaddressed`.
    """
    msgs = await tail(limit=max_messages, max_tokens=max_tokens)
    pending = await unaddressed(msgs)
    return {"messages": msgs, "unaddressed": pending}
