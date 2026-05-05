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
        # Address-claim fallback: if the model responded but didn't
        # claim any user message (no mark_addressed calls AND no
        # explicit respond.addresses), default to claiming every
        # currently-unaddressed user message in the window. Without
        # this, the supervisor sees the same message still pending on
        # the next tick and fires AGAIN — operator gets a duplicate
        # reply for one prompt.
        addr_set = list(final_response.get("addresses") or addressed)
        if not addr_set and pending:
            addr_set = [p.get("id") for p in pending if p.get("id")]
            # Stamp tally-marks so future fires also see them addressed.
            for uid in addr_set:
                try:
                    await thread_mod.mark_addressed(
                        user_message_id=uid,
                        note="auto-claimed by harness response",
                        by="harness-auto-claim",
                    )
                except Exception as e:
                    print(f"[threaded-fire] auto-claim mark failed for {uid[:12]}: {type(e).__name__}: {e}")
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

    # Phase 5 review pass — separate focused turn whose only job is
    # to consider whether the working set deserves provenance. Splits
    # the answer concern from the consolidation concern; the model
    # gets a stripped prompt with one job. Soft-fails — review failure
    # never breaks the main response.
    if final_response and final_response.get("body"):
        try:
            await _run_review_pass(
                session_tag=session_tag,
                pending=pending,
                final_body=final_response["body"],
                tool_history=_tool_history_summary(chat_msgs),
            )
        except Exception as e:
            print(f"[threaded-fire] review pass crashed: {type(e).__name__}: {e}")

    return {
        "final_response": final_response,
        "turns": turns_used,
        "addressed": addressed,
        "lake_id": lake_id,
    }


def _salvage_tool_call_from_content(content: str) -> dict | None:
    """If the model emitted a propose_provenance call as JSON content
    instead of using the native tool_calls field, extract its args.

    Provider quirk — Gemini in particular sometimes wraps tool calls
    in the legacy envelope shape {"kind": "tool_call", "tool": "...",
    "args": {...}} as content rather than going through tool_calls.
    Returns the args dict or None.
    """
    if not content:
        return None
    # Strip a leading code fence if present.
    s = content.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: s.rfind("```")].strip()
    # Find a top-level JSON object (greedy from the first { to the matching }).
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        envelope = json.loads(s[start:end])
    except Exception:
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("tool") != "propose_provenance":
        return None
    args = envelope.get("args")
    if not isinstance(args, dict):
        return None
    return args


def _tool_history_summary(chat_msgs: list[dict]) -> str:
    """Render the fire's tool calls + results as a flat text block
    for the review prompt. Includes recall hits inline so the model
    sees the actual delta ids it pulled and can cite them in
    propose_provenance.from_ids."""
    lines: list[str] = []
    pending_calls: dict[str, dict] = {}
    for m in chat_msgs:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                tcid = tc.get("id") or ""
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                args = fn.get("arguments") or "{}"
                pending_calls[tcid] = {"name": name, "args": args}
        elif role == "tool":
            tcid = m.get("tool_call_id") or ""
            call = pending_calls.pop(tcid, None) or {"name": "?", "args": "{}"}
            content = (m.get("content") or "")
            lines.append(f"  · {call['name']}({call['args']})")
            # Truncate result so a 50KB recall doesn't bloat the prompt.
            result = content if len(content) <= 1500 else content[:1500] + f"\n…[truncated {len(content) - 1500} chars]"
            lines.append(f"    → {result}")
    return "\n".join(lines) if lines else "  (no tool calls)"


async def _run_review_pass(
    *,
    session_tag: str,
    pending: list[dict],
    final_body: str,
    tool_history: str,
) -> None:
    """Fire one focused turn whose only job is to decide whether the
    fire's working set deserves a provenance proposal.

    Mirrors the legacy `_run_post_response_review` but on the threaded
    chat protocol — system message contains REVIEW_SYSTEM, the only
    tool available is propose_provenance. The model either emits
    that tool_call (we dispatch it) or returns plain content (we skip).
    """
    from .prompts import REVIEW_SYSTEM

    # Pure-standpoint fires (no tool calls) have nothing to consolidate.
    if not tool_history.strip() or tool_history.strip() == "(no tool calls)":
        return

    # Use the most recent unaddressed question as the seed text. If we
    # already addressed everything, take the first pending entry as a
    # representative.
    if not pending:
        return
    question = (pending[0].get("content") or "").strip()
    if not question:
        return

    system_text = REVIEW_SYSTEM.format(
        question=question,
        answer=final_body,
        tool_history=tool_history,
    )

    # Only propose_provenance is exposed — the review pass can't
    # search, dispatch, or address anything. One job.
    review_tools = [
        s for s in tool_schemas.chat_tools()
        if s.get("function", {}).get("name") == "propose_provenance"
    ]

    messages = [
        {"role": "system", "content": system_text},
        {
            "role": "user",
            "content": "Review this fire. Call propose_provenance if a stretch deserves naming, otherwise reply with a one-sentence skip reason and no tool call.",
        },
    ]

    try:
        asst = await loop_generate_chat(
            messages=messages,
            tools=review_tools,
            tool_choice="auto",
            max_tokens=1500,
        )
    except Exception as e:
        print(f"[threaded-review] LLM call crashed: {type(e).__name__}: {e}")
        return

    tool_calls = asst.get("tool_calls") or []
    if not tool_calls:
        # Some providers (most notably Gemini for review-pass-shaped
        # prompts) emit the tool call as JSON content rather than
        # using the native tool_calls field. Parse and route from
        # content as a fallback.
        content = (asst.get("content") or "").strip()
        salvaged = _salvage_tool_call_from_content(content)
        if salvaged is not None:
            try:
                result = await tool_schemas.dispatch(
                    name="propose_provenance",
                    args=salvaged,
                    session_tag=session_tag,
                )
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"
            await _write_threaded_turn_trace(
                session_tag=session_tag,
                turn=0,
                tool="propose_provenance",
                thinking=f"(salvaged from content fallback) {content[:200]}",
                args=salvaged,
                result=result,
            )
            return
        skip_reason = content
        await _write_threaded_turn_trace(
            session_tag=session_tag,
            turn=0,
            tool="(review-skip)",
            thinking=skip_reason[:300],
            result=skip_reason[:300] or "no tool call emitted",
        )
        return

    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if name != "propose_provenance":
            continue
        args = _parse_tool_args(tc)
        try:
            result = await tool_schemas.dispatch(
                name="propose_provenance",
                args=args,
                session_tag=session_tag,
            )
        except Exception as e:
            result = f"ERROR: {type(e).__name__}: {e}"
        await _write_threaded_turn_trace(
            session_tag=session_tag,
            turn=0,
            tool="propose_provenance",
            thinking=(asst.get("content") or "")[:300],
            args=args,
            result=result,
        )


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
        from ... import delta_client as lake
        from ..puddle import puddle
        from ..intents import CONVO_TAG, Q_A_TTL_S
        import json as _json
        from datetime import UTC, datetime, timedelta

        payload = _json.dumps({
            "kicker": "",
            "title": "",
            "body": body,
            "tail": "",
        })
        # Lake side first so we have a durable id to point the puddle
        # mirror at via lake-id:<id>. Without the lake write the
        # rehydrate's `feed-card` slice on the next api restart finds
        # nothing and the assistant turn vanishes from the dashboard
        # — only the user message (which has its own lake-side
        # composer write) reappears.
        lake_tags = [
            "feed-card",
            "synthesis",
            "addressing-output",
            "route:chat-reply",
        ]
        for addr in addresses:
            lake_tags.append(f"addresses:{addr}")
        # Match witness's TTL so feed-cards age out of the lake the
        # same way regardless of which harness produced them.
        feed_card_ttl = Q_A_TTL_S
        bridge_lake_id = ""
        try:
            lake_delta = await lake.write(
                content=payload,
                tags=lake_tags,
                source="harness-threaded",
                expires_at=(datetime.now(UTC) + timedelta(seconds=feed_card_ttl)).isoformat(),
            )
            if isinstance(lake_delta, dict):
                bridge_lake_id = lake_delta.get("id") or ""
        except Exception as le:
            print(f"[threaded-fire] bridge lake write failed: {type(le).__name__}: {le}")

        tags = [CONVO_TAG] + lake_tags
        if lake_id:
            tags.append(f"lake-id:{lake_id}")
            tags.append(f"recalled-id:{lake_id[:24]}")
        if bridge_lake_id:
            # Mark the puddle copy with the bridge's own lake id so
            # rehydrate's recalled-id dedup recognizes them as the
            # same row on cold-start replay.
            tags.append(f"recalled-id:{bridge_lake_id[:24]}")
        await puddle.write(
            content=payload,
            tags=tags,
            source="harness-threaded",
            ttl_seconds=Q_A_TTL_S,
        )
    except Exception as e:
        print(f"[threaded-fire] puddle bridge failed: {type(e).__name__}: {e}")
