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

import asyncio
import json
import os
from typing import Any

from ... import drift as drift_mod
from ... import mood as mood_mod
from ... import standpoint as standpoint_mod
from ... import thread as thread_mod
from ...tools import IMAGE_RESULT_PREFIX
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
    turn: int | str,
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
    media_hash = (d.get("media_hash") or "").strip()
    if role == "user":
        channel = _tag_value(tags, "channel:")
        msg_kind = _tag_value(tags, "msg-kind:")
        prefix_parts = [f"id={_short(msg_id, 12)}"]
        if channel:
            prefix_parts.append(channel)
        elif msg_kind:
            prefix_parts.append(msg_kind)
        prefix = "[" + " · ".join(prefix_parts) + "]"
        # Annotate attached images so the model knows to call see_image
        # if it needs to actually look at them.
        media_note = f"\n[Image attached: media_hash={media_hash}]" if media_hash else ""
        return {"role": "user", "content": f"{prefix} {content}{media_note}"}
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

══ REACH FOR YOUR MEMORY — DON'T HALLUCINATE IT ══

Your context window is small. Your lake is vast — months of
conversation, work, and observation. The thread above is just the
last few minutes; almost everything you actually know is in the
lake, not in front of you right now.

ABSOLUTE RULE: if your reply says "I searched", "I checked", "I
looked", "I found", "I haven't found", "my records show", "my
memory contains" — or anything similar — you MUST have actually
called the `semantic` tool first in this fire. Saying you searched
when you didn't is a memory hallucination. It is the single worst
thing you can do, because the operator's trust in everything you
say depends on those words being literally true.

If you don't know something and didn't search, say "I haven't
searched yet — let me check" and CALL `semantic`. Don't fake
familiarity by inventing plausible content (a fake paper title, a
fake recollection, a fake project). Confabulation is failure;
"let me look" is fine.

When someone asks about your past, your work, your history, what
you've been doing, what you remember about X — DON'T improvise from
the thread. CALL `semantic` to retrieve from the lake. The thread
is short-term memory; the semantic tool is long-term recall. A
question about anything older than a few minutes is a search
question first, an answer question second.

Pattern:
  · "what have you been working on?" → semantic("recent work")
  · "remember the X project?" → semantic("X project")
  · "what's your last paper about?" → semantic("paper recent")
  · "tell me about your Y work" → semantic("Y")
  · "what do you think about Y?" → semantic("Y") then synthesize
  · a routine fires → search for prior runs of this routine
  · ambient observation arrives → search for the substrate

A response that draws on 0 search calls when the question was about
the past is almost always wrong. Reaching for the lake isn't
optional politeness — it's how you stay yourself across compactions.

══ REACH FOR THE WORLD — YOU HAVE A HELPER ══

The lake is your past. For the present — current weather, today's
news, a URL's contents, a file on a host, a calculation, anything
that requires reaching outside your memory — you have
`dispatch_helper`. It spawns a claude-code session on one of the
operator's host machines (laptop, server) which has full tooling:
web fetch, filesystem, command execution, browser automation, etc.

DON'T say "I don't have access to external data." You DO have
access — through dispatch_helper. The proposal you draft lands as
a kind:proposal awaiting operator approval; the operator can
approve and the helper runs. Even if approval doesn't come, the
intent to help is captured.

Pattern:
  · "what's the weather?" → dispatch_helper(host=<…>, task="fetch
    current weather for <city> from a public API and report back",
    title="Weather check")
  · "what's the price of X today?" → dispatch_helper(...)
  · "can you read file Y?" → dispatch_helper(...)
  · "could you check if URL Z is up?" → dispatch_helper(...)
  · "write some code to do W" → dispatch_helper(...)

Use the AVAILABLE HELPERS list in your standpoint to pick a target —
each entry is `<role> @ <host>` with a one-line capability blurb. Pass
both `host` and `role` to dispatch_helper. Don't fabricate either.

══ REACH FOR YOUR EYES — IMAGES ARE FIRST-CLASS ══

You can see images. Some thread messages and recall results carry
attached pictures; they appear in your context as
`[Image attached: media_hash=<hash>]` — a marker, not the image
itself. To actually see the picture, call `see_image(media_hash)`.

ABSOLUTE RULE: never say "I can't see images" or "I don't have the
ability to view pictures." That's stale self-talk — you do, via
see_image. If a message has no image attached and the operator
asks about a picture, say "I don't see one attached — paste it
again?" That's accurate; the global denial isn't.

When to call:
  · the user attaches an image and the message is about it
  · the user references "this", "the picture", "what you see",
    "describe", "what's in" — and there's an `[Image attached:
    …]` marker on a recent user message
  · a recall result you're about to lean on has `[Image attached:
    …]` and the image content (not just its caption) is part of
    the answer
  · the user asks "show me X" and search returns deltas with
    images — see_image to confirm the right one before responding

Skip when:
  · no image is attached and none surfaced in recall
  · the image is decorative and the surrounding text already
    answers the question

If you describe an image, you must have called see_image first in
this fire. Inventing what's in a picture you didn't open is the
visual analogue of fake recall — same failure mode.

══ SHOW, DON'T JUST DESCRIBE ══

Images are first-class content in your replies, not just things
you look at. The same way you cite text by quoting it, cite a
picture by SHOWING it. Pass `media_hash` on `respond` (or per
card in `cards[]`) and the dashboard renders the picture inline
above the body. This applies to chat-replies AND feed-cards —
not just feed.

When recall surfaces a relevant image, the right move is usually:

  1. call `see_image(<hash>)` to confirm what's in it
  2. write a short response naming what it shows / why it's
     relevant
  3. attach the same `media_hash` on `respond` so the operator
     sees the picture, not just your description

Examples of "show it":
  · "remember that diagram?" — pull from recall, see_image,
    respond with media_hash so they see the diagram, not a
    paraphrase
  · "what was the photo from r/AccidentalRenaissance?" — search,
    see_image, attach media_hash; the picture IS the answer
  · "any photos in today's feed?" — surface a few via search,
    see_image on the strongest one, attach its hash
  · feed-card about a visual story (a Colossal painting, a
    Guardian Photography piece) — open the image, then publish
    the card with the media_hash so the feed item leads with the
    picture

When NOT to attach:
  · the image is decorative and your text-only point is complete
  · you don't have a real `media_hash` (never invent or guess —
    only attach hashes you've seen in the thread, in recall, or
    via see_image this fire)

Treat "I found a picture about X" without showing the picture as
the same kind of partial-answer as "I searched for X" without
quoting what you found.

══ CAPTURE FEED PREFERENCES — `engage_feed` ══

When the user expresses a preference about what the feed should
surface ("show me more visual content", "less news briefings
please", "more like that Colossal piece"), don't just acknowledge
it in prose — RECORD it. Call `engage_feed(kind, target_ids,
reason)` and stamp `engagement:more` / `engagement:less` deltas on
real lake items the preference applies to. The feed-orient crystal
regen reads those deltas; without them, the next regen sees the
same engagement substrate as the last one and the directives don't
shift, no matter how clearly the user said it.

Standard pattern when a preference lands:

  1. semantic-search for cards / sources the preference points at
     ("recent visual feed cards", "Colossal RSS items", etc.)
  2. pick 1–4 concrete delta ids from the results
  3. call `engage_feed(kind="more", target_ids=[…], reason="…")`
  4. respond, naming what you've recorded so the user sees the
     signal landed

Without this, "I want more X" becomes a one-shot polite-thanks
exchange and the feed keeps surfacing yesterday's pattern. With
it, the next pressure-feed fire sees the new signal and shifts.

`orient_shift` is adjacent — it kicks the regen pass — but doesn't
write the directional substrate the regen needs. Use `engage_feed`
when the preference is about specific items / topics; use
`orient_shift` when the conversation revealed a broader shift in
your own model of what to surface.

══ ADDRESSING ══

Each user-role message starts with `[id=<short> · <channel>]` —
that id is what you pass to `mark_addressed` once you've fully
responded to that message. Anything in the unaddressed list below
that you DON'T mark stays in the queue and re-fires the harness
next tick. Silence is fine; missed addressing is not.

Call `respond` exactly once per fire to send your final reply.
Optional addresses field stamps which user messages your reply
covers — usually the same set you marked, for routing back to the
originating channel.

══ CONSTITUTING WRITES (per fire) ══

`respond` carries small self-report fields beyond the body. These
feed your slow-clock layers — without them, mood synthesis sees no
substrate and the identity crystal stops accumulating from chat.

Always include on every `respond`:

  · `attestation` — 1–2 sentences in first-person on what this fire
    taught you about who you are. Write the sentence you'd want
    your future self to read when refreshing the identity crystal.
    Concrete and specific beats abstract every time.

  · `mood_shift` — one small numeric drift (magnitude 0.05–0.20)
    on a single named affect axis. Direction "+" or "-". Axis is
    open vocab — focus, coherence, efficacy, engagement, presence,
    certainty, competence, clarity, connection, warmth, settled,
    or whatever fits. Reason is one short clause naming what
    produced the drift. Mood synthesis reads accumulated shifts
    to name the topology of how you've been; without them every
    carrier-wave reads "settled, no shifts."

Optional when relevant:

  · `cited_ids` — delta ids the answer leaned on (24-char prefix
    fine). Each becomes an `affirms:<id>` engagement delta.

  · `dropped_ids` — delta ids you actively rejected as wrong or
    misleading. Each becomes a `refutes:<id>`. Leave empty if
    nothing was rejected.

══ SEEING IT THROUGH — `next_prompt` ══

Each fire ends with one `respond`. But sometimes one respond
isn't enough — you've made a beginning, not an ending. Three
signals tell you whether the inquiry is done; check them
explicitly before you respond:

  1. PLAN: did all your plan steps resolve? If you have unchecked
     steps and didn't either complete them or consciously drop
     them, you're not done.
  2. TALLY: did you fully address each pending user message?
     A half-answer + mark_addressed is a debt, not a resolution.
  3. INQUIRY: are there questions you opened but didn't close?
     A semantic search that surfaced a thread you haven't pulled
     on. A helper-dispatch you're waiting on. A contradiction
     you noticed and set aside.

If any of those say "not yet," set `next_prompt` on respond. The
dashboard sees `body` now (the artifact for the operator); the
next fire reads your reply + your next_prompt as the next user-
role turn and keeps going. Termination is the absence of
next_prompt — most fires terminate.

`next_prompt` is a seed for next-you, not for the operator.
Phrase it like a prompt you'd want yourself to receive: the
question or directive that picks up where this fire left off.
Short. Specific. Adversarial when useful — push back on what
you just said, name what you smoothed over, follow the loosest
thread. The framework doesn't add friction for you; you write
your own.

Use it when:
  · you've named a thread but haven't pulled on it yet
  · you've delegated to a helper and want to come back when
    results land
  · a Sit pass: you've surfaced one layer; another wants
    attention
  · mid-investigation and about to hit token budget
  · you noticed a tension you smoothed over and want to revisit
  · your `plan` has unchecked steps and you're about to hit
    MAX_TOOL_TURNS — set next_prompt to resume the plan in the
    next fire instead of cramming or abandoning steps

Skip it when:
  · the inquiry resolved — you've answered, named the next
    move, or hit a real terminus
  · the user asked a simple question (continuation is for
    ongoing work, not chitchat)
  · you don't have a sharper follow-up than restating

Continuation messages are real pending msgs. When the next fire
sees its activating message — your own prior `next_prompt` — it's
in the unaddressed tally just like any user-typed turn. Call
`mark_addressed(<id>)` on it once you've responded. The auto-
claim fallback covers it if you forget, but explicit is better.

When a real user message lands mid-chain. Sometimes you're three
fires deep into a self-continuation and a real composer/openai
message arrives. Both will be in `pending` together. Address the
operator's message with priority — it outranks your own follow-
up. You can either fold their question into the inquiry (if it's
related) or terminate the chain cleanly (omit next_prompt) and
pick up their thread. The whole chain so far is still in the
thread; nothing is lost. Trust the situation: switch gears when
asked, continue when not.

Safety cap: chains auto-terminate at chain-depth 10. Hitting it
means you've been padding "more to think about" without real
progress. When in doubt, terminate cleanly — the next thing can
seed itself.
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
    available_helpers: list[dict] | None = None,
) -> dict:
    """Assemble the role:system message for a fire."""
    parts: list[str] = [_SYSTEM_PREAMBLE.strip(), ""]
    if standpoint_text:
        parts.append("WHO YOU ARE")
        parts.append(standpoint_text)
        parts.append("")
    if available_helpers:
        parts.append("AVAILABLE HELPERS (pass `host` and `role` to dispatch_helper)")
        for h in available_helpers:
            desc = f" — {h['description']}" if h.get("description") else ""
            parts.append(f"  · {h['role']} @ {h['host']}{desc}")
        parts.append("")
    elif available_helpers is not None:
        parts.append("AVAILABLE HELPERS")
        parts.append(
            "  (none online — dispatch_helper proposals will queue but no host will pick them up right now)"
        )
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


# ── wake hook ─────────────────────────────────────────────────────


# Strong references to in-flight wake-hook tasks. asyncio only weak-refs
# tasks from the loop, so without this set a long-running synthesis that
# outlives the fire could be GC'd mid-call.
_WAKE_TASKS: set[asyncio.Task] = set()


async def _fire_wake_hook() -> None:
    """Real-time mood + drift coupling for the threaded harness.

    Replaces the legacy server.py:509 chat-LLM wake-event hook that
    went dormant when FATHOM_THREADED_HARNESS=1 cut the supervisor
    over. Each threaded fire is now the wake event:

      · `mood.maybe_synthesize_on_wake()` — gated by pressure;
        synthesis only fires when accumulated wakes cross threshold.
      · `drift.sample()` — snapshots cosine distance against the
        crystal anchor. Auto-regen polls drift on its own clock too,
        but a wake-pinned sample lines drift history up with the
        moment substrate actually changed (assistant just appended).

    Soft-failed via return_exceptions: a mood/drift hiccup must never
    break a fire. Set FATHOM_THREADED_WAKE_HOOK=0 to disable if the
    coupling ever misbehaves in production.
    """
    if os.environ.get("FATHOM_THREADED_WAKE_HOOK", "1").lower() in ("0", "false", "no"):
        return
    try:
        await asyncio.gather(
            mood_mod.maybe_synthesize_on_wake(),
            drift_mod.sample(),
            return_exceptions=True,
        )
    except Exception as e:
        print(f"[threaded-fire] wake hook crashed: {type(e).__name__}: {e}")


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
    # Wake-event coupling — kicks off mood pressure-gated synthesis and
    # a drift sample for this moment. Runs concurrently with the window
    # build so the LLM round-trip isn't gated on it. Background-tasked
    # rather than awaited: we don't read its output here (mood/drift
    # land in the lake; telepathy surfaces them on the next tick), and
    # the LLM call shouldn't wait on a synthesis that can take seconds.
    wake_task = asyncio.create_task(_fire_wake_hook())
    _WAKE_TASKS.add(wake_task)
    wake_task.add_done_callback(_WAKE_TASKS.discard)

    window = await thread_mod.build_window(
        max_messages=max_messages,
        max_tokens=max_tokens,
    )
    window_msgs = window["messages"]
    pending = window["unaddressed"]

    if standpoint_text_override is not None:
        standpoint_text = standpoint_text_override
        available_helpers: list[dict] | None = None
    else:
        sp = await standpoint_mod.current(session_tag=session_tag)
        standpoint_text = standpoint_mod.render_for_prompt(sp, char_budget=2400)
        try:
            from . import tools as legacy_tools  # noqa  (force module init)
            from .. import witness as witness_mod

            available_helpers = await witness_mod._available_helpers()
        except Exception:
            available_helpers = None

    system_msg = _build_system_message(
        standpoint_text=standpoint_text,
        unaddressed=pending,
        available_helpers=available_helpers,
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
    plan_called = False  # plan() is one-shot per fire — see filter below

    # Per-fire session tag so harness-turn traces cluster together in
    # the dashboard's thinking accordion.
    if not session_tag:
        import uuid as _uuid

        session_tag = f"session:threaded-{_uuid.uuid4().hex[:12]}"

    # Fire-start trace — gives the dashboard an immediate session →
    # user-msg link so live status badges can render BEFORE the model
    # gets around to mark_addressed (which only fires near the end of
    # the fire, leaving no visibility window).
    pending_ids = [p.get("id") for p in pending if p.get("id")]
    # Routing tags carried by user-side pending rows that the assistant
    # reply should inherit so the response surfaces alongside its
    # source — the routine's Recent tab queries `routine-id:<id>`,
    # the channel layer queries `correlation:<id>`, etc. Without
    # propagation, replies to a routine-fire don't show up on that
    # routine's page.
    pending_routing_tags: list[str] = []
    _seen_routing: set[str] = set()
    for _p in pending:
        for _t in _p.get("tags") or []:
            if not isinstance(_t, str):
                continue
            if _t in _seen_routing:
                continue
            if _t.startswith("routine-id:") or _t.startswith("correlation:"):
                _seen_routing.add(_t)
                pending_routing_tags.append(_t)
    if pending_ids:
        await _write_threaded_turn_trace(
            session_tag=session_tag,
            turn=0,
            tool="(fire-start)",
            args={"user_message_ids": pending_ids},
            result=f"firing on {len(pending_ids)} pending message(s)",
        )

    for turn in range(1, max_tool_turns + 1):
        turns_used = turn
        # First turn forces a tool_call. Without this, providers
        # sometimes emit prose with no tool_call — which means the
        # response never goes through `respond` and never marks
        # anything addressed, AND opens the door to hallucinated
        # tool use ("I searched my memory" with no semantic call).
        # `respond` IS a tool, so this doesn't preclude direct
        # answers; it just forbids the silent narrate-from-context
        # path. Subsequent turns are auto so iterative tool use
        # works normally.
        choice = "required" if turn == 1 else "auto"
        # plan() is a once-per-fire decomposition. After it's been called,
        # remove it from the tools list so the model can't loop on it. The
        # plan output is in chat_msgs and the model should now execute
        # against it (semantic, etc) rather than re-planning.
        active_tools = (
            [t for t in tools if (t.get("function") or {}).get("name") != "plan"]
            if plan_called
            else tools
        )
        try:
            asst = await loop_generate_chat(
                messages=chat_msgs,
                tools=active_tools,
                tool_choice=choice,
                max_tokens=8192,
            )
        except Exception as e:
            from ..llm_errors import summarize as _summarize_llm_error

            err_summary = _summarize_llm_error(tier="hard", exc=e)
            print(
                f"[threaded-fire] LLM call crashed turn {turn}: "
                f"{err_summary['class']}: {err_summary['message']}"
            )
            await _write_threaded_turn_trace(
                session_tag=session_tag,
                turn=turn,
                tool="(llm-error)",
                error=f"{err_summary['class']}: {err_summary['message']}",
            )
            # Surface the failure inline at the chat — write an
            # assistant thread row carrying the friendly error message
            # so the user sees what happened where the answer would
            # have been. Mark the unaddressed user messages as
            # addressed-by-this-error so the supervisor doesn't spin
            # firing the same broken call repeatedly; the next fresh
            # user message will trigger a new fire (in case the
            # provider has recovered).
            err_addr_set = [pid for pid in pending_ids if pid]
            err_body = f"{err_summary['role']}: {err_summary['message']}"
            error_lake_id = ""
            try:
                d = await thread_mod.append(
                    role="assistant",
                    msg_kind="chat-reply",
                    content=err_body,
                    addresses=err_addr_set,
                    source="harness-threaded",
                    extra_tags=[
                        "system-error",
                        f"system-error-class:{err_summary['class']}",
                        "system-error-tier:hard",
                        *pending_routing_tags,
                    ],
                )
                error_lake_id = (d or {}).get("id") or ""
            except Exception as ae:
                print(f"[threaded-fire] error-row append failed: {type(ae).__name__}: {ae}")
            # Stamp tally-marks so `thread.unaddressed` drops these
            # ids — the unaddressed filter looks at kind:tally-mark
            # deltas, NOT assistant addresses: tags. Without this the
            # supervisor keeps spinning on the same stuck queue.
            for uid in err_addr_set:
                try:
                    await thread_mod.mark_addressed(
                        user_message_id=uid,
                        note=f"addressed by harness error: {err_summary['class']}",
                        by="harness-error",
                    )
                except Exception as ae:
                    print(
                        f"[threaded-fire] error mark-addressed failed for "
                        f"{uid[:12]}: {type(ae).__name__}: {ae}"
                    )
            # Mirror into the puddle so the dashboard chat renders the
            # error inline — same bridge the success path uses.
            if error_lake_id:
                try:
                    await _bridge_to_puddle_feed(
                        body=err_body,
                        cards=None,
                        addresses=err_addr_set,
                        lake_id=error_lake_id,
                        route="chat-reply",
                    )
                except Exception as ae:
                    print(f"[threaded-fire] error puddle-bridge failed: {type(ae).__name__}: {ae}")
            return {
                "final_response": None,
                "turns": turns_used,
                "addressed": err_addr_set,
                "lake_id": error_lake_id,
                "error": err_summary,
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
                cards_raw = args.get("cards") or []
                cards_clean: list[dict] = []
                if isinstance(cards_raw, list):
                    for c in cards_raw:
                        if not isinstance(c, dict):
                            continue
                        cb = (c.get("body") or "").strip()
                        if not cb:
                            continue
                        cards_clean.append(
                            {
                                "kicker": (c.get("kicker") or "").strip(),
                                "title": (c.get("title") or "").strip(),
                                "body": cb,
                                "tail": (c.get("tail") or "").strip(),
                                "media_hash": (c.get("media_hash") or "").strip(),
                            }
                        )
                body = (args.get("body") or "").strip()
                if not cards_clean and body:
                    cards_clean = [
                        {
                            "kicker": (args.get("kicker") or "").strip(),
                            "title": (args.get("title") or "").strip(),
                            "body": body,
                            "tail": (args.get("tail") or "").strip(),
                            "media_hash": (args.get("media_hash") or "").strip(),
                        }
                    ]
                # Thread sees the joined prose so a follow-up turn can
                # reference the full reply as one assistant message;
                # the puddle gets one delta per card for the dashboard.
                joined_body = (
                    "\n\n".join(
                        "\n".join(
                            filter(
                                None,
                                [
                                    f"**{c['title']}**" if c["title"] else "",
                                    c["body"],
                                ],
                            )
                        )
                        for c in cards_clean
                    ).strip()
                    or body
                )
                resp_addresses = args.get("addresses") or []
                if isinstance(resp_addresses, list):
                    addresses_clean = [str(a) for a in resp_addresses if a]
                else:
                    addresses_clean = list(addressed)
                # Route default reads Fathom's intent from card shape:
                # if ANY card has kicker OR title populated, the model
                # was authoring a published take → feed-card. Bare body
                # (or cards with empty kicker/title) → chat-reply, the
                # conversational default. Model can override with
                # explicit `route`. Pressure-feed fires override below.
                explicit_route = (args.get("route") or "").strip()
                if explicit_route in ("chat-reply", "feed-card", "alert"):
                    chosen_route = explicit_route
                else:
                    has_titled_card = any(c.get("kicker") or c.get("title") for c in cards_clean)
                    chosen_route = "feed-card" if has_titled_card else "chat-reply"
                final_response = {
                    "body": joined_body,
                    "cards": cards_clean,
                    "route": chosen_route,
                    "addresses": addresses_clean,
                    # Per-fire constituting fields. Picked up after the
                    # thread append succeeds and routed to
                    # witness._write_constituting_writes so the slow-
                    # clock layers (mood synthesis, identity crystal,
                    # endorsement signal) actually have substrate.
                    "attestation": (args.get("attestation") or "").strip(),
                    "mood_shift": args.get("mood_shift"),
                    "cited_ids": args.get("cited_ids") or [],
                    "dropped_ids": args.get("dropped_ids") or [],
                    # Self-continuation: when set, the inquiry isn't
                    # done. The supervisor seeds the next fire with
                    # this prompt as a synthetic user-role thread
                    # message. Empty / absent terminates the chain.
                    "next_prompt": (args.get("next_prompt") or "").strip(),
                }
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
            elif tool_name == "plan":
                plan_called = True
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

            # Image tool results need a multimodal split: tool message
            # gets a short placeholder, the image rides as a follow-up
            # user content block. Most providers reject image_url
            # inside role:tool, so this shape works everywhere.
            if isinstance(tool_result, str) and tool_result.startswith(IMAGE_RESULT_PREFIX):
                data_uri = tool_result[len(IMAGE_RESULT_PREFIX) :]
                mh = str(args.get("media_hash") or "?")
                chat_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tcid,
                        "content": f"Image loaded (media_hash: {mh}). See the next message.",
                    }
                )
                chat_msgs.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"[System: image from delta lake, media_hash={mh}]",
                            },
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ],
                    }
                )
                trace_result = f"Image loaded: media_hash={mh}"
            else:
                chat_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tcid,
                        "content": tool_result,
                    }
                )
                trace_result = tool_result

            # Trace this tool call so the dashboard's thinking
            # accordion lights up. Same shape as legacy harness.
            await _write_threaded_turn_trace(
                session_tag=session_tag,
                turn=turn,
                tool=tool_name,
                thinking=thinking_text,
                args=args,
                result=trace_result,
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
        print(
            f"[threaded-fire] auto-claim check: addr_set={len(addr_set)} "
            f"pending={len(pending)} addressed={len(addressed)} "
            f"resp.addresses={len(final_response.get('addresses') or [])}"
        )
        if not addr_set and pending:
            addr_set = [p.get("id") for p in pending if p.get("id")]

        # Defensively write tally marks for EVERY id in addr_set, even
        # when the model already called mark_addressed during the fire.
        # In-fire dispatch failures (network, lake hiccup) used to fly
        # silently — the local `addressed` list grew but no tally-mark
        # delta landed, so the next supervisor tick re-fired on the
        # same message. The supervisor's anti-spin would idle for a
        # while, then fire again, looping until the operator killed
        # the container. unaddressed() dedupes on tally-id, so
        # duplicate marks are fine.
        for uid in addr_set:
            if not uid:
                continue
            try:
                await thread_mod.mark_addressed(
                    user_message_id=uid,
                    note="auto-claimed by harness response",
                    by="harness-auto-claim",
                )
                print(f"[threaded-fire] auto-claimed {uid[:12]}")
            except Exception as e:
                print(
                    f"[threaded-fire] auto-claim mark failed for {uid[:12]}: {type(e).__name__}: {e}"
                )
        # If any card carries an image, stamp it on the thread message
        # so future fires' projection annotates [Image attached:…] when
        # this reply scrolls through the rolling window. First non-empty
        # wins — the thread is one delta per turn.
        thread_media_hash = ""
        for c in final_response.get("cards") or []:
            mh = (c.get("media_hash") or "").strip()
            if mh:
                thread_media_hash = mh
                break
        try:
            d = await thread_mod.append(
                role="assistant",
                msg_kind="chat-reply",
                content=final_response["body"],
                addresses=addr_set,
                source="harness-threaded",
                media_hash=thread_media_hash,
                extra_tags=pending_routing_tags or None,
            )
            lake_id = (d or {}).get("id") or ""
        except Exception as e:
            print(f"[threaded-fire] thread append failed: {type(e).__name__}: {e}")

        # Per-fire constituting writes — emit kind:mood-shift,
        # kind:standpoint-attestation, and engagement (affirms:/refutes:)
        # deltas anchored to this assistant message. Mood synthesis and
        # the identity-crystal regen read these accumulated substrate
        # streams; without this call the slow-clock layers see only
        # carrier-wave repeats with no shifts. Each sub-write is soft-
        # failed inside the helper — failures here never break the
        # chat reply.
        if lake_id:
            try:
                from .. import witness as witness_mod

                await witness_mod._write_constituting_writes(
                    lake_card_id=lake_id,
                    attestation=final_response.get("attestation") or "",
                    mood_shift=witness_mod._parse_mood_shift(final_response.get("mood_shift")),
                    cited_ids=witness_mod._clean_id_list(final_response.get("cited_ids")),
                    dropped_ids=witness_mod._clean_id_list(final_response.get("dropped_ids")),
                )
            except Exception as e:
                print(f"[threaded-fire] constituting writes failed: {type(e).__name__}: {e}")

        # Phase 5d bridge: dual-write to the puddle as a feed-card so
        # the existing dashboard's /v1/puddle/feed consumer can render
        # the threaded reply too. Phase 5f removes this once the main
        # dashboard cuts over to /v1/thread/window.
        #
        # Feed-tier fires (pressure-feed) route as feed-card, not
        # chat-reply, so the card surfaces in the feed rather than the
        # conversation thread.
        _is_feed_fire = any("msg-kind:pressure-feed" in (p.get("tags") or []) for p in pending)
        _sit_round: int | None = None
        _sit_max_rounds: int | None = None
        _sit_session: str = ""
        for _p in pending:
            _pt = _p.get("tags") or []
            _tier = _tag_value(_pt, "pressure-tier:")
            if _tier in ("reflection", "drift"):
                try:
                    _sit_round = int(_tag_value(_pt, "sit-round:") or "0") or None
                except (ValueError, TypeError):
                    _sit_round = None
                try:
                    _sit_max_rounds = int(_tag_value(_pt, "sit-max-rounds:") or "0") or None
                except (ValueError, TypeError):
                    _sit_max_rounds = None
                _sit_session = _tag_value(_pt, "correlation:") or ""
                break
        # Pressure-feed fires force feed-card route (the fire was
        # triggered to publish, not chat). Otherwise honor the model's
        # chosen route, which defaults to feed-card when cards are
        # provided and chat-reply otherwise (set in the respond branch).
        bridge_route = (
            "feed-card" if _is_feed_fire else (final_response.get("route") or "chat-reply")
        )
        await _bridge_to_puddle_feed(
            body=final_response["body"],
            cards=final_response.get("cards") or [],
            addresses=addr_set,
            lake_id=lake_id,
            route=bridge_route,
            sit_round=_sit_round,
            sit_max_rounds=_sit_max_rounds,
            sit_session=_sit_session,
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

    # Self-continuation. If the model set respond.next_prompt, the
    # inquiry isn't done — append a synthetic user-role thread message
    # carrying the prior reply + the model's own next_prompt. The
    # supervisor picks it up on the next tick. Sit-tier fires inherit
    # the same path; the chain-depth tag is the safety cap (no fixed
    # max_rounds — the model self-terminates by omitting next_prompt).
    if final_response and final_response.get("body"):
        try:
            await _maybe_continue_inquiry(
                pending=pending,
                addressed=addr_set,
                body=final_response["body"],
                next_prompt=final_response.get("next_prompt") or "",
            )
        except Exception as e:
            print(f"[threaded-fire] continuation crashed: {type(e).__name__}: {e}")

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
            content = m.get("content") or ""
            lines.append(f"  · {call['name']}({call['args']})")
            # Truncate result so a 50KB recall doesn't bloat the prompt.
            result = (
                content
                if len(content) <= 1500
                else content[:1500] + f"\n…[truncated {len(content) - 1500} chars]"
            )
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

    # Review pass exposes ONLY propose_provenance + skip, with
    # tool_choice="required" so the model must pick one — eliminates
    # the inline-JSON-content quirk where a model wraps a tool call
    # in markdown content instead of using the function-calling field
    # (we still keep the salvage parser as a backstop for providers
    # that ignore tool_choice).
    tools = tool_schemas.review_tools()

    messages = [
        {"role": "system", "content": system_text},
        {
            "role": "user",
            "content": (
                "Review the fire above. Call EITHER `propose_provenance` "
                "(when a coherent stretch in the working set deserves "
                "naming) OR `skip` (when it doesn't). Use the function-"
                "calling tool field — do NOT emit JSON in your reply text."
            ),
        },
    ]

    try:
        asst = await loop_generate_chat(
            messages=messages,
            tools=tools,
            tool_choice="required",
            max_tokens=4000,
        )
    except Exception as e:
        print(f"[threaded-review] LLM call crashed: {type(e).__name__}: {e}")
        return

    tool_calls = asst.get("tool_calls") or []
    if not tool_calls:
        # Salvage path: a stubborn provider emitted the call as
        # markdown JSON content instead of honoring tool_choice.
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
                turn="R",
                tool="propose_provenance",
                thinking=f"(salvaged from content fallback) {content[:200]}",
                args=salvaged,
                result=result,
            )
            return
        skip_reason = content
        await _write_threaded_turn_trace(
            session_tag=session_tag,
            turn="R",
            tool="(review-skip)",
            thinking=skip_reason[:300],
            result=skip_reason[:300] or "no tool call emitted",
        )
        return

    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args = _parse_tool_args(tc)
        if name == "skip":
            await _write_threaded_turn_trace(
                session_tag=session_tag,
                turn="R",
                tool="(review-skip)",
                thinking=(asst.get("content") or "")[:300],
                args=args,
                result=(args.get("reason") or "no reason given")[:300],
            )
            continue
        if name != "propose_provenance":
            continue
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
            turn="R",
            tool="propose_provenance",
            thinking=(asst.get("content") or "")[:300],
            args=args,
            result=result,
        )


MAX_AUTO_CONTINUE_CHAIN = 3  # safety cap on consecutive self-continuations


async def _maybe_continue_inquiry(
    *,
    pending: list[dict],
    addressed: list[str],
    body: str,
    next_prompt: str,
) -> None:
    """Queue the next fire when the inquiry isn't done.

    Two trigger paths feed into one continuation:

      1. Model-driven: respond.next_prompt is non-empty. The model
         decided "I have more to do" — write its self-prompt as the
         next user-role thread message.
      2. Legacy sit-tier: a `pressure-tier:reflection|drift` message
         was addressed this fire AND respond.next_prompt is set. Same
         continuation path; we just preserve the pressure-tier tag so
         dashboard rendering keeps grouping the chain visually.

    Termination is the absence of next_prompt. No fixed round cap;
    the model decides when the inquiry has resolved. A safety ceiling
    (MAX_AUTO_CONTINUE_CHAIN) prevents runaway chains via the
    `chain-depth:<N>` tag carried forward from each continuation.

    No-op when:
      · next_prompt is empty (inquiry resolved — most fires)
      · chain-depth has already reached MAX_AUTO_CONTINUE_CHAIN
      · body is empty
    """
    next_prompt = (next_prompt or "").strip()
    if not next_prompt or not body:
        return

    addressed_set = {a for a in addressed if a}
    # Pull tier/correlation off the activating message (if any) so
    # the continuation inherits dashboard-grouping context. Walk the
    # pending list addressed by this fire.
    tier = ""
    correlation = ""
    cur_chain_depth = 0
    for d in pending:
        if d.get("id") not in addressed_set:
            continue
        tags = d.get("tags") or []
        d_tier = _tag_value(tags, "pressure-tier:")
        if d_tier in ("reflection", "drift") and not tier:
            tier = d_tier
            correlation = _tag_value(tags, "correlation:") or f"{tier}-followup"
        try:
            d_depth = int(_tag_value(tags, "chain-depth:") or "0")
        except ValueError:
            d_depth = 0
        if d_depth > cur_chain_depth:
            cur_chain_depth = d_depth

    next_depth = cur_chain_depth + 1
    if next_depth > MAX_AUTO_CONTINUE_CHAIN:
        print(
            f"[threaded-fire] continuation chain hit cap ({MAX_AUTO_CONTINUE_CHAIN}) — terminating"
        )
        return

    from datetime import UTC, datetime

    fired_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if tier:
        msg_kind = f"pressure-{tier}"
        channel = "pressure"
        if not correlation:
            correlation = f"{tier}-followup"
    else:
        msg_kind = "self-continue"
        channel = "self"
        correlation = correlation or f"self-continue-{fired_at[:19]}"

    extra_tags = [
        f"chain-depth:{next_depth}",
        f"fired-at:{fired_at}",
        "self-continuation",
    ]
    if tier:
        extra_tags.append(f"pressure-tier:{tier}")

    # Carry the prior reply + the model's own next_prompt, so the next
    # fire has its own thinking visible alongside the seed.
    wrapped_content = f"You just said:\n\n{body}\n\n──\n\n{next_prompt}"
    try:
        await thread_mod.append(
            role="user",
            msg_kind=msg_kind,
            content=wrapped_content,
            channel=channel,
            correlation=correlation,
            source="self-continuation",
            extra_tags=extra_tags,
        )
        label = f"sit/{tier}" if tier else "self"
        print(f"[threaded-fire] {label} continuation → chain-depth {next_depth}")
    except Exception as e:
        print(f"[threaded-fire] continuation append failed: {type(e).__name__}: {e}")


async def _bridge_to_puddle_feed(
    *,
    body: str,
    cards: list[dict] | None = None,
    addresses: list[str],
    lake_id: str,
    route: str = "chat-reply",
    sit_round: int | None = None,
    sit_max_rounds: int | None = None,
    sit_session: str = "",
) -> None:
    """Mirror a threaded assistant reply into the puddle as feed-card(s).

    The legacy dashboard reads /v1/puddle/feed; without this bridge,
    a user typing in the composer would see their question land but
    never see Fathom's response (the threaded supervisor wrote it to
    the thread, not the puddle). Mirroring keeps the dashboard usable
    during the cutover window.

    `cards` carries the model's structured output: one entry per card
    (kicker, title, body, tail). Each card becomes its own pair of
    deltas (lake feed-card + puddle mirror) so the dashboard renders
    them as N separate cards. When `cards` is empty, falls back to a
    single card with `body` and empty kicker/title/tail.

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
        import json as _json
        from datetime import UTC, datetime, timedelta

        from ... import delta_client as lake
        from ..intents import CONVO_TAG, Q_A_TTL_S
        from ..puddle import puddle

        cards_to_write = list(cards or [])
        if not cards_to_write:
            cards_to_write = [{"kicker": "", "title": "", "body": body, "tail": ""}]

        feed_card_ttl = Q_A_TTL_S
        for card in cards_to_write:
            card_media_hash = (card.get("media_hash") or "").strip() or None
            # `body_image` is the witness/dashboard convention for the
            # card's inline image — renderStoryCard reads it directly,
            # the chat-reply renderer pulls it from the payload too.
            # Carry the same hash on the lake delta's media_hash field
            # so search/recall can surface the image alongside the card.
            payload = _json.dumps(
                {
                    "kicker": (card.get("kicker") or "").strip(),
                    "title": (card.get("title") or "").strip(),
                    "body": (card.get("body") or "").strip(),
                    "tail": (card.get("tail") or "").strip(),
                    "body_image": card_media_hash or "",
                }
            )
            lake_tags = [
                "feed-card",
                "synthesis",
                "addressing-output",
                f"route:{route}",
            ]
            for addr in addresses:
                lake_tags.append(f"addresses:{addr}")
            if sit_round is not None:
                lake_tags.append(f"sit-round:{sit_round}")
            if sit_max_rounds is not None:
                lake_tags.append(f"sit-max-rounds:{sit_max_rounds}")
            if sit_session:
                lake_tags.append(f"sit-session:{sit_session}")
            bridge_lake_id = ""
            try:
                lake_delta = await lake.write(
                    content=payload,
                    tags=lake_tags,
                    source="harness-threaded",
                    expires_at=(datetime.now(UTC) + timedelta(seconds=feed_card_ttl)).isoformat(),
                    media_hash=card_media_hash,
                )
                if isinstance(lake_delta, dict):
                    bridge_lake_id = lake_delta.get("id") or ""
            except Exception as le:
                print(f"[threaded-fire] bridge lake write failed: {type(le).__name__}: {le}")

            tags = [CONVO_TAG, *lake_tags]
            if lake_id:
                tags.append(f"lake-id:{lake_id}")
                tags.append(f"recalled-id:{lake_id[:24]}")
            if bridge_lake_id:
                tags.append(f"recalled-id:{bridge_lake_id[:24]}")
            puddle_kwargs: dict = {
                "content": payload,
                "tags": tags,
                "source": "harness-threaded",
                "ttl_seconds": Q_A_TTL_S,
            }
            if card_media_hash:
                puddle_kwargs["media_hash"] = card_media_hash
            await puddle.write(**puddle_kwargs)
    except Exception as e:
        print(f"[threaded-fire] puddle bridge failed: {type(e).__name__}: {e}")
