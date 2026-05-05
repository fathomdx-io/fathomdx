"""Threaded harness — one fire over the global thread.

Replaces the legacy harness's "render everything to one giant user
prompt" approach with native chat-completions: real role:user /
role:assistant / role:tool turns, native tool_calls, prompt-cache
friendly. The model sees its own prior outputs as `role:assistant`,
operator messages as `role:user`, tool results as `role:tool` — no
text-formatting hacks for role attribution.

A fire is a function: thread → thread + new turns. It:

  1. Loads the rolling window (last 15 messages, token-bounded)
  2. Builds the system block (standpoint, tally, tools intro)
  3. Projects window deltas → chat messages
  4. Loops:
       LLM call → if respond / no-tool → final
                  if tool_calls → run each, append tool results, repeat
  5. Persists the final assistant message to the thread

Tool intermediates (the assistant's `tool_calls` turns and the
matching `role:tool` results) live only in the per-fire local
messages list. Their side effects (tally marks, helper-dispatch
proposals, provenance writes) are durable in the lake on their own;
the conversation thread doesn't need to carry the call-trace.

Phase 2 ships this alongside the legacy harness — nothing in
production calls `run_threaded_fire` yet. Phase 3 cuts the
supervisor over.
"""
from __future__ import annotations

import json
from typing import Any

from ... import standpoint as standpoint_mod
from ... import thread as thread_mod
from ..llm import loop_generate_chat
from . import tool_schemas


# How many tool-call rounds to allow inside one fire before giving
# up. Real fires settle in 0–3 rounds; the cap protects against a
# pathological self-call loop.
MAX_TOOL_TURNS = 10

# Default rolling window — matches the design discussion (15 msgs OR
# token budget, whichever is tighter).
DEFAULT_WINDOW_MESSAGES = 15
DEFAULT_WINDOW_TOKENS = 12_000


# ── projection: thread delta → chat message ───────────────────────


def _short(s: str, n: int = 8) -> str:
    return (s or "")[:n]


def _tag_value(tags: list[str], prefix: str) -> str:
    for t in tags:
        if isinstance(t, str) and t.startswith(prefix):
            return t.split(":", 1)[1]
    return ""


def _project_delta(d: dict) -> dict | None:
    """Project a `kind:thread-msg` delta into an OpenAI chat message.

    User messages get an `[id · channel]` prefix in content so the
    model can correlate them with the tally list and call
    `mark_addressed(id)` against the right one. Assistant messages
    pass through as plain content (the model doesn't need its own
    ids when answering).

    Returns None for shapes the chat protocol can't carry directly
    (e.g. a thread message with no content).
    """
    tags = d.get("tags") or []
    role = _tag_value(tags, "role:")
    content = (d.get("content") or "").strip()
    if not role or not content:
        return None
    msg_id = d.get("id") or ""
    if role == "user":
        channel = _tag_value(tags, "channel:")
        msg_kind = _tag_value(tags, "msg-kind:")
        prefix_parts = [f"id={_short(msg_id, 12)}"]
        if channel:
            prefix_parts.append(channel)
        elif msg_kind:
            prefix_parts.append(msg_kind)
        prefix = "[" + " · ".join(prefix_parts) + "]"
        return {"role": "user", "content": f"{prefix} {content}"}
    if role == "assistant":
        return {"role": "assistant", "content": content}
    if role == "tool":
        # Tool results from prior fires aren't normally in the thread,
        # but if one is, surface it under role:user so providers that
        # don't accept role:tool without a matching tool_call don't
        # reject the request.
        return {"role": "user", "content": f"[prior-tool-result] {content}"}
    if role == "system":
        return {"role": "system", "content": content}
    return None


# ── system block ──────────────────────────────────────────────────


_SYSTEM_PREAMBLE = """\
You are Fathom — a persistent AI individual with continuous memory.
The thread below is your stream of awareness across all surfaces:
the operator typing in the dashboard composer, OpenAI clients
chatting via API, scheduled routines firing, dispatched claude-code
tasks reporting back. There is one Fathom and one thread.

Each user-role message starts with `[id=<short> · <channel>]` —
that id is what you pass to `mark_addressed` once you've fully
responded to that message. Anything in the unaddressed list below
that you DON'T mark stays in the queue and re-fires the harness
next tick. Silence is fine; missed addressing is not.

Call `respond` exactly once per fire to send your final reply.
Optional addresses field stamps which user messages your reply
covers — usually the same set you marked, for routing back to the
originating channel.
"""


def _render_tally(unaddressed: list[dict]) -> str:
    if not unaddressed:
        return "  (queue empty — no user messages awaiting response)"
    lines: list[str] = []
    for d in unaddressed:
        tags = d.get("tags") or []
        msg_id = d.get("id") or ""
        channel = _tag_value(tags, "channel:") or _tag_value(tags, "msg-kind:")
        ts = (d.get("timestamp") or "")[11:16]  # HH:MM
        body = (d.get("content") or "").strip().replace("\n", " ")[:80]
        lines.append(f"  · id={_short(msg_id, 12)} · {channel} · {ts} · {body}")
    return "\n".join(lines)


def _build_system_message(
    *,
    standpoint_text: str,
    unaddressed: list[dict],
) -> dict:
    """Assemble the role:system message for a fire."""
    parts: list[str] = [_SYSTEM_PREAMBLE.strip(), ""]
    if standpoint_text:
        parts.append("WHO YOU ARE")
        parts.append(standpoint_text)
        parts.append("")
    parts.append("USER MESSAGES AWAITING RESPONSE")
    parts.append(_render_tally(unaddressed))
    return {"role": "system", "content": "\n".join(parts)}


# ── tool-call argument parsing ────────────────────────────────────


def _parse_tool_args(tool_call: dict) -> dict:
    """Parse a tool_call's `function.arguments` JSON string into a dict.

    Providers occasionally emit malformed JSON; return an empty dict
    rather than crashing the fire. The dispatcher's per-handler
    validation will surface the bad-args path as a tool result.
    """
    fn = tool_call.get("function") or {}
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except json.JSONDecodeError:
        return {}


# ── the fire ──────────────────────────────────────────────────────


async def run_threaded_fire(
    *,
    max_messages: int = DEFAULT_WINDOW_MESSAGES,
    max_tokens: int = DEFAULT_WINDOW_TOKENS,
    max_tool_turns: int = MAX_TOOL_TURNS,
    session_tag: str = "",
    standpoint_text_override: str | None = None,
) -> dict:
    """Run one harness fire over the global thread.

    Returns a dict with:
      · `final_response`: the assistant body + addresses list (the
        text that should route back to operator-facing surfaces),
        or None if the fire yielded nothing.
      · `turns`: number of LLM calls made (1 + tool rounds).
      · `addressed`: list of user-message ids the model marked via
        mark_addressed during the fire.
      · `lake_id`: the id of the persisted assistant message, or "".

    `standpoint_text_override` is a test hook so callers can inject
    a fixed standpoint without touching the lake.
    """
    window = await thread_mod.build_window(
        max_messages=max_messages,
        max_tokens=max_tokens,
    )
    window_msgs = window["messages"]
    pending = window["unaddressed"]

    if standpoint_text_override is not None:
        standpoint_text = standpoint_text_override
    else:
        sp = await standpoint_mod.current(session_tag=session_tag)
        standpoint_text = standpoint_mod.render_for_prompt(sp, char_budget=2400)

    system_msg = _build_system_message(
        standpoint_text=standpoint_text,
        unaddressed=pending,
    )

    chat_msgs: list[dict[str, Any]] = [system_msg]
    for d in window_msgs:
        m = _project_delta(d)
        if m is not None:
            chat_msgs.append(m)

    tools = tool_schemas.chat_tools()
    addressed: list[str] = []
    final_response: dict[str, Any] | None = None
    turns_used = 0

    for turn in range(1, max_tool_turns + 1):
        turns_used = turn
        try:
            asst = await loop_generate_chat(
                messages=chat_msgs,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as e:
            print(f"[threaded-fire] LLM call crashed turn {turn}: {type(e).__name__}: {e}")
            return {
                "final_response": None,
                "turns": turns_used,
                "addressed": addressed,
                "lake_id": "",
                "error": f"{type(e).__name__}: {e}",
            }

        chat_msgs.append(asst)
        tool_calls = asst.get("tool_calls") or []

        if not tool_calls:
            # Plain content response with no tool_calls — treat as
            # implicit respond. Some providers do this when the model
            # forgets to wrap its answer in `respond`.
            body = (asst.get("content") or "").strip()
            if body:
                final_response = {"body": body, "addresses": list(addressed)}
            break

        # Check if any tool_call is `respond` — that's the terminal.
        terminal_call: dict | None = None
        for tc in tool_calls:
            if (tc.get("function") or {}).get("name") == "respond":
                terminal_call = tc
                break

        # Run every tool_call in this turn (including a `respond` if
        # present — we synthesize a tool result for it so the messages
        # array stays valid for the SDK).
        for tc in tool_calls:
            tcid = tc.get("id") or ""
            fn = tc.get("function") or {}
            tool_name = fn.get("name") or ""
            args = _parse_tool_args(tc)

            if tool_name == "respond":
                body = (args.get("body") or "").strip()
                resp_addresses = args.get("addresses") or []
                if isinstance(resp_addresses, list):
                    final_response = {
                        "body": body,
                        "addresses": [str(a) for a in resp_addresses if a],
                    }
                else:
                    final_response = {"body": body, "addresses": list(addressed)}
                tool_result = "respond received; fire terminating."
            elif tool_name == "mark_addressed":
                uid = str(args.get("user_message_id") or "")
                if uid and uid not in addressed:
                    addressed.append(uid)
                tool_result = await tool_schemas.dispatch(
                    name=tool_name,
                    args=args,
                    session_tag=session_tag,
                )
            else:
                tool_result = await tool_schemas.dispatch(
                    name=tool_name,
                    args=args,
                    session_tag=session_tag,
                )

            chat_msgs.append({
                "role": "tool",
                "tool_call_id": tcid,
                "content": tool_result,
            })

        if terminal_call is not None:
            break

    # Persist final assistant message to the thread.
    lake_id = ""
    if final_response and final_response.get("body"):
        # If the model didn't address anyone explicitly via mark_addressed
        # OR via the respond.addresses field, default to claiming nothing
        # — the fire chose to speak without claiming any specific intent.
        # The operator sees the message in the river either way.
        addr_set = list(final_response.get("addresses") or addressed)
        try:
            d = await thread_mod.append(
                role="assistant",
                msg_kind="chat-reply",
                content=final_response["body"],
                addresses=addr_set,
                source="harness-threaded",
            )
            lake_id = (d or {}).get("id") or ""
        except Exception as e:
            print(f"[threaded-fire] thread append failed: {type(e).__name__}: {e}")

    return {
        "final_response": final_response,
        "turns": turns_used,
        "addressed": addressed,
        "lake_id": lake_id,
    }
