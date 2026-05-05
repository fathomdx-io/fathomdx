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
from ..intents import CONVO_TAG
from ..llm import loop_generate_chat
from ..puddle import puddle as _puddle
from . import tool_schemas


_TRACE_ARGS_CAP = 1500
_TRACE_RESULT_CAP = 1500


def _truncate(s: str, cap: int) -> str:
    if not s or len(s) <= cap:
        return s
    return s[:cap] + f"\n…[truncated {len(s) - cap} chars]"


async def _write_threaded_turn_trace(
    *,
    session_tag: str,
    turn: int,
    tool: str,
    thinking: str = "",
    args: dict | None = None,
    result: str = "",
    error: str = "",
) -> None:
    """Mirror legacy `_write_turn_trace` so the dashboard's thinking
    accordion lights up for threaded fires too. Same shape, same
    `kind:harness-turn` tag, same JSON envelope — the existing feed
    renderer (api/loop/routes.py) handles both paths uniformly.

    Soft-fails: trace visibility is decoration, never load-bearing.
    """
    args_blob = ""
    if args is not None:
        try:
            args_blob = _truncate(
                json.dumps(args, ensure_ascii=False, default=str),
                _TRACE_ARGS_CAP,
            )
        except Exception:
            args_blob = "<unserializable args>"
    payload = {
        "turn": turn,
        "tool": tool,
        "thinking": (thinking or "")[:600],
        "args_json": args_blob,
        "result": _truncate(result or "", _TRACE_RESULT_CAP),
        "error": error or "",
        "plan_step": None,
    }
    body = json.dumps(payload, ensure_ascii=False, default=str)
    tags = [
        CONVO_TAG,
        session_tag,
        "kind:harness-turn",
        f"tool:{tool}",
        f"turn:{turn}",
        "harness-source:threaded",
    ]
    if error:
        tags.append("turn-error")
    try:
        await _puddle.write(
            content=body,
            tags=tags,
            source="harness-trace",
            ttl_seconds=6 * 60 * 60,
        )
    except Exception as e:
        print(f"[threaded-trace] puddle write failed: {type(e).__name__}: {e}")


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

    # Per-fire session tag so harness-turn traces cluster together in
    # the dashboard's thinking accordion.
    if not session_tag:
        import uuid as _uuid
        session_tag = f"session:threaded-{_uuid.uuid4().hex[:12]}"

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
            await _write_threaded_turn_trace(
                session_tag=session_tag,
                turn=turn,
                tool="(llm-error)",
                error=f"{type(e).__name__}: {e}",
            )
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
        thinking_text = (asst.get("content") or "").strip()
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

            # Trace this tool call so the dashboard's thinking
            # accordion lights up. Same shape as legacy harness.
            await _write_threaded_turn_trace(
                session_tag=session_tag,
                turn=turn,
                tool=tool_name,
                thinking=thinking_text,
                args=args,
                result=tool_result,
            )
            # Only attribute the model's pre-tool-call thinking text
            # to the FIRST tool call this turn — subsequent calls in
            # the same turn don't have separate thinking.
            thinking_text = ""

        # If the model emitted plain content (no tool_calls), trace it
        # too so the operator can see the implicit-respond path.
        if not tool_calls and thinking_text:
            await _write_threaded_turn_trace(
                session_tag=session_tag,
                turn=turn,
                tool="(implicit-respond)",
                thinking=thinking_text,
                result=thinking_text,
            )

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

        # Phase 5d bridge: dual-write to the puddle as a feed-card so
        # the existing dashboard's /v1/puddle/feed consumer can render
        # the threaded reply too. Phase 5f removes this once the main
        # dashboard cuts over to /v1/thread/window.
        await _bridge_to_puddle_feed(
            body=final_response["body"],
            addresses=addr_set,
            lake_id=lake_id,
        )

    return {
        "final_response": final_response,
        "turns": turns_used,
        "addressed": addressed,
        "lake_id": lake_id,
    }


async def _bridge_to_puddle_feed(
    *,
    body: str,
    addresses: list[str],
    lake_id: str,
) -> None:
    """Mirror a threaded assistant reply into the puddle as a feed-card.

    The legacy dashboard reads /v1/puddle/feed; without this bridge,
    a user typing in the composer would see their question land but
    never see Fathom's response (the threaded supervisor wrote it to
    the thread, not the puddle). Mirroring keeps the dashboard usable
    during the cutover window.

    Tag shape mirrors what witness._dispatch_card writes for a
    chat-reply card: feed-card + route:chat-reply + addresses fan-out.
    Source is `harness-threaded` so the renderer can distinguish
    threaded-origin cards from witness-origin ones if needed.

    Bridge addresses are the THREAD user-msg ids, not the puddle
    intent ids — that's a feature, not a bug. The dashboard's feed
    renderer will show the user message (puddle intent) and the
    Fathom response (puddle feed-card) by chronological order; the
    addresses linkage is decorative on the card UI side.
    """
    try:
        from ..puddle import puddle
        from ..intents import CONVO_TAG, Q_A_TTL_S
        import json as _json

        payload = _json.dumps({
            "kicker": "",
            "title": "",
            "body": body,
            "tail": "",
        })
        tags = [
            CONVO_TAG,
            "feed-card",
            "synthesis",
            "addressing-output",
            "route:chat-reply",
        ]
        for addr in addresses:
            tags.append(f"addresses:{addr}")
        if lake_id:
            tags.append(f"lake-id:{lake_id}")
            tags.append(f"recalled-id:{lake_id[:24]}")
        await puddle.write(
            content=payload,
            tags=tags,
            source="harness-threaded",
            ttl_seconds=Q_A_TTL_S,
        )
    except Exception as e:
        print(f"[threaded-fire] puddle bridge failed: {type(e).__name__}: {e}")
