"""Harness driver — the agentic loop that replaces the convener+parliament+witness pipeline.

The loop reads the same starting context the witness reads today
(standpoint + identity/mood anchors + conversation feed + pending
intents + available hosts/routines), but instead of a fixed pipeline,
hands the model a tool harness and lets it decide what additional
substrate to gather (or not) before responding.

Exit condition: model emits `kind:respond`. Every other response shape
either dispatches a tool or is treated as malformed and the turn
budget is consumed. Hard cap on turns is a safety, not a typical exit —
most fires complete in 0–2 tool calls.

On the response, dispatch piggybacks on `witness._dispatch_card` for
each card. This keeps the harness from re-implementing routing, lake
writes, judge scheduling, channel resolution — all the witness card-
dispatch surface area stays where it is. Constituting writes
(attestation, mood-shift, citation deltas) likewise reuse the witness
helper.
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Awaitable, Callable

from .. import witness as witness_mod
from ..intents import CONVO_TAG, intent_kind
from ..llm import loop_generate
from ..puddle import puddle
from .prompts import HARNESS_SYSTEM, render_tool_history
from .tools import TOOL_HANDLERS, TOOL_MODEL_ARGS


MAX_TURNS = 8                # hard cap on tool calls per fire
MAX_TOKENS_PER_TURN = 4096   # response budget — final card needs room


# Event callback signature: `(event_type: str, payload: dict) -> None | Awaitable[None]`.
# Used by harness_test's SSE endpoint to stream the loop's progress to a
# visualizer in real time. Production callers (worker.py) pass None.
EventCallback = Callable[[str, dict], Any]


async def _emit(cb: EventCallback | None, event: str, payload: dict) -> None:
    """Fire an event callback if one was provided. Soft-fails on
    callback exceptions so a buggy visualizer can't break the loop."""
    if cb is None:
        return
    try:
        result = cb(event, payload)
        if inspect.isawaitable(result):
            await result
    except Exception as e:
        print(f"[harness] event callback raised on {event!r}: {type(e).__name__}: {e}")


# ─── entry point ───────────────────────────────────────────────────────


async def run_harness(
    *,
    session_tag: str,
    pending: list[dict],
    voice_order: list[str] | None = None,  # back-compat: accepted but unused
    standpoint=None,
    event_callback: EventCallback | None = None,
) -> list[str]:
    """Run one full harness fire — drop-in replacement for `run_witness`.

    Returns the list of intent-ids the harness claims to have addressed,
    same return shape as `witness.run_witness`. `voice_order` is accepted
    for signature compatibility but ignored — the harness elects its own
    deliberation via the `deliberate` tool, so an outer parliament order
    isn't meaningful here.

    `event_callback` (optional) is called at key points in the loop with
    structured payloads. Used by the harness-test visualizer to stream
    progress; production callers (worker.py) pass None.
    """
    if not pending:
        return []
    cb = event_callback
    await _emit(cb, "start", {
        "session_tag": session_tag,
        "intent_count": len(pending),
        "max_turns": MAX_TURNS,
    })

    # Pre-render the persistent context blocks that don't change across
    # tool-call turns within this fire. Re-rendered once per turn so
    # telepathy refreshes (anchors) or new conversation feed entries
    # land mid-loop, but cheap enough that we don't cache.
    intent_block, short_to_full = _render_intent_block(pending)
    available_hosts = await witness_mod._available_claude_code_hosts()
    hosts_block = witness_mod._render_hosts_block(available_hosts)
    routines_block = await witness_mod._render_routines_block(available_hosts)
    # The harness's design principle: the model sees everything that's
    # available. The shared render_for_prompt has internal per-item
    # caps (identity [:600], endorsement excerpts [:80], understanding
    # [:160], and only the first 6 endorsements / 3 understanding
    # entries). For the harness we want full state, so we render
    # straight off the standpoint object.
    if standpoint is not None:
        standpoint_block = _render_standpoint_full(standpoint)
    else:
        standpoint_block = "(standpoint unavailable — speak from anchors and feed)"

    tool_history: list[dict] = []
    final_response: dict | None = None

    await _emit(cb, "context_built", {
        "intent_block": intent_block,
        "standpoint_block": standpoint_block,
        "hosts_block": hosts_block,
        "routines_block": routines_block,
    })

    for turn in range(1, MAX_TURNS + 1):
        # Re-render anchors + feed each turn so telepathy refreshes
        # (which run on a slow clock) land if they happen mid-fire.
        # Harness-native versions — no per-item truncation.
        anchors_block = _render_anchors_full()
        feed_block = _render_conversation_feed_full(session_tag)

        prompt = HARNESS_SYSTEM.format(
            standpoint_block=standpoint_block,
            anchors_block=anchors_block,
            feed_block=feed_block,
            intent_block=intent_block,
            hosts_block=hosts_block,
            routines_block=routines_block,
            tool_history=render_tool_history(tool_history),
            turn_number=turn,
            max_turns=MAX_TURNS,
        )

        await _emit(cb, "turn_begin", {"turn": turn, "prompt_chars": len(prompt)})

        try:
            raw = await loop_generate(
                prompt=prompt,
                tier="hard",
                max_tokens=MAX_TOKENS_PER_TURN,
                temperature=0.7,
                json_mode=True,
            )
        except Exception as e:
            print(f"[harness] LLM call crashed turn {turn}: {type(e).__name__}: {e}")
            await _emit(cb, "error", {"where": "llm_call", "turn": turn,
                                      "type": type(e).__name__, "message": str(e)})
            return []

        parsed = _parse_envelope(raw)
        if parsed is None:
            print(
                f"[harness] turn {turn} unparseable — "
                f"raw[:200]={raw[:200]!r} raw[-120:]={raw[-120:]!r}"
            )
            tool_history.append({
                "turn": turn, "tool": "(parse-error)", "args": {},
                "error": "response was not valid JSON envelope",
            })
            await _emit(cb, "parse_error", {"turn": turn, "raw_head": raw[:200], "raw_tail": raw[-120:]})
            continue

        kind = (parsed.get("kind") or "").strip()

        if kind == "respond":
            final_response = parsed
            print(f"[harness] turn {turn}: RESPOND ({len(parsed.get('cards') or [])} cards)")
            await _emit(cb, "respond", {
                "turn": turn,
                "cards": parsed.get("cards") or [],
                "attestation": parsed.get("attestation") or "",
                "mood_shift": parsed.get("mood_shift"),
                "cited_ids": parsed.get("cited_ids") or [],
                "dropped_ids": parsed.get("dropped_ids") or [],
            })
            break

        if kind == "tool_call":
            tool_name = (parsed.get("tool") or "").strip()
            args_raw = parsed.get("args") or {}
            thinking = (parsed.get("thinking") or "").strip()
            if thinking:
                print(f"[harness] turn {turn}: {tool_name} — {thinking[:120]}")
            else:
                print(f"[harness] turn {turn}: {tool_name}")
            await _emit(cb, "tool_call", {
                "turn": turn, "tool": tool_name, "args": args_raw, "thinking": thinking,
            })
            entry = await _dispatch_tool(
                turn=turn,
                tool_name=tool_name,
                args=args_raw,
                session_tag=session_tag,
                pending=pending,
                standpoint=standpoint,
            )
            tool_history.append(entry)
            await _emit(cb, "tool_result", {
                "turn": turn, "tool": tool_name,
                "result": entry.get("result"),
                "error": entry.get("error"),
            })
            continue

        # Unknown kind — log and consume the turn.
        print(f"[harness] turn {turn} unknown kind {kind!r}")
        tool_history.append({
            "turn": turn, "tool": "(unknown-kind)", "args": {},
            "error": f"unknown envelope kind: {kind!r}",
        })
        await _emit(cb, "unknown_kind", {"turn": turn, "kind": kind, "raw": parsed})

    if final_response is None:
        print(f"[harness] hit max turns ({MAX_TURNS}) without responding — silent fire")
        await _emit(cb, "max_turns_reached", {"max_turns": MAX_TURNS})
        return []

    addressed = await _dispatch_response(
        response=final_response,
        pending=pending,
        short_to_full=short_to_full,
        session_tag=session_tag,
        voice_order=voice_order,
    )
    await _emit(cb, "done", {"addressed": addressed})
    return addressed


# ─── tool dispatch ─────────────────────────────────────────────────────


async def _dispatch_tool(
    *,
    turn: int,
    tool_name: str,
    args: dict,
    session_tag: str,
    pending: list[dict],
    standpoint,
) -> dict:
    """Run one tool call. Returns the history entry to append."""
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {
            "turn": turn, "tool": tool_name, "args": args,
            "error": f"unknown tool {tool_name!r} — valid: {sorted(TOOL_HANDLERS)}",
        }
    if not isinstance(args, dict):
        return {
            "turn": turn, "tool": tool_name, "args": {},
            "error": f"args must be a JSON object, got {type(args).__name__}",
        }
    allowed = TOOL_MODEL_ARGS.get(tool_name, set())
    cleaned = {k: v for k, v in args.items() if k in allowed}

    # Inject harness-context kwargs the model doesn't supply directly.
    if tool_name == "deliberate":
        cleaned["session_tag"] = session_tag
        cleaned["pending"] = pending
        cleaned["standpoint"] = standpoint

    try:
        result = await handler(**cleaned)
    except TypeError as e:
        return {
            "turn": turn, "tool": tool_name, "args": cleaned,
            "error": f"bad args — {e}",
        }
    except Exception as e:
        return {
            "turn": turn, "tool": tool_name, "args": cleaned,
            "error": f"{type(e).__name__}: {e}",
        }
    return {
        "turn": turn, "tool": tool_name, "args": cleaned,
        "result": result,
    }


# ─── response dispatch ─────────────────────────────────────────────────


async def _dispatch_response(
    *,
    response: dict,
    pending: list[dict],
    short_to_full: dict[str, str],
    session_tag: str,
    voice_order: list[str] | None,
) -> list[str]:
    """Hand the model's final cards to the witness's existing dispatcher.

    Re-uses `witness._dispatch_card` per card and `_write_constituting_writes`
    for the fire-level attestation/mood-shift/citation writes, anchored
    to the first card's lake-id (same convention the witness uses today).
    """
    cards_raw = response.get("cards") or []
    available_hosts = await witness_mod._available_claude_code_hosts()
    primary_intent = (pending[0].get("content") or "").strip() if pending else ""

    cards: list[dict] = []
    for card in cards_raw:
        if not isinstance(card, dict):
            continue
        body = (card.get("body") or "").strip()
        if not body:
            continue
        tool_args_raw = card.get("tool_args")
        cards.append({
            "kicker": (card.get("kicker") or "").strip(),
            "title": (card.get("title") or "").strip(),
            "body": body,
            "tail": (card.get("tail") or "").strip(),
            "body_image": (card.get("body_image") or "").strip(),
            "link": (card.get("link") or "").strip(),
            "links": card.get("links") or [],
            "route": (card.get("route") or "chat-reply").strip(),
            "addresses": card.get("addresses") or [],
            "tool": (card.get("tool") or "").strip(),
            "tool_args": tool_args_raw if isinstance(tool_args_raw, dict) else {},
        })

    if not cards:
        print("[harness] respond payload had no usable cards — silent fire")
        return []

    full_addressed_union: list[str] = []
    seen_addressed: set[str] = set()
    first_lake_id: str = ""
    for card in cards:
        lake_id, claimed = await witness_mod._dispatch_card(
            card=card,
            pending=pending,
            short_to_full=short_to_full,
            available_hosts=available_hosts,
            session_tag=session_tag,
            primary_intent=primary_intent,
            voice_order=voice_order,
        )
        for cid in claimed:
            if cid and cid not in seen_addressed:
                seen_addressed.add(cid)
                full_addressed_union.append(cid)
        if lake_id and not first_lake_id:
            first_lake_id = lake_id

    cited_ids = witness_mod._clean_id_list(response.get("cited_ids"))

    if first_lake_id:
        try:
            await witness_mod._write_constituting_writes(
                lake_card_id=first_lake_id,
                attestation=(response.get("attestation") or "").strip(),
                mood_shift=witness_mod._parse_mood_shift(response.get("mood_shift")),
                cited_ids=cited_ids,
                dropped_ids=witness_mod._clean_id_list(response.get("dropped_ids")),
            )
        except Exception as e:
            print(f"[harness] constituting-act writes failed: {type(e).__name__}: {e}")

        # Q/A marker — record that this question was asked and the answer
        # leaned on these citations. Written alongside fresh recall on
        # future fires, not in place of it. After enough markers stack
        # up on a recurring topic, a higher-level provenance can fold
        # them into a "you've been here many times" summary.
        try:
            await _write_qa_marker(
                pending=pending,
                cards=cards,
                cited_ids=cited_ids,
                lake_card_id=first_lake_id,
            )
        except Exception as e:
            print(f"[harness] qa-marker write failed: {type(e).__name__}: {e}")

    return full_addressed_union


async def _write_qa_marker(
    *,
    pending: list[dict],
    cards: list[dict],
    cited_ids: list[str],
    lake_card_id: str,
) -> None:
    """Write a Q/A provenance marker after the harness responds.

    Records that this question was asked, what was said, and which
    deltas the answer leaned on. Future fires that resonate with this
    question will surface this marker as additional context — not as
    a cache replacing fresh recall, but as recognition that the
    question has been visited before.

    Skipped when:
      - No pending intent text (no question to anchor on)
      - No cards (silent / NEIFAMA fire — nothing said)
      - No cited_ids (no provenance to mark over)

    The marker carries `kind:provenance` so it surfaces through the
    same ascend/expand machinery as the hand-curated hierarchy. Tagged
    `provenance-level:0` to sit BELOW level-1 episodes — these are
    pre-episode, question-anchored, prone to being folded up later.
    """
    from ... import delta_client

    if not pending or not cards or not cited_ids:
        return

    question = (pending[0].get("content") or "").strip()
    question = question.split("\n\n[intent-payload]", 1)[0].strip()
    if not question:
        return

    # Combine card bodies into one answer text. Most fires emit one
    # card; multi-card fires (chat-reply + feed-card) get concatenated.
    answer_parts = [c.get("body", "").strip() for c in cards if c.get("body")]
    answer = "\n\n".join(p for p in answer_parts if p)
    if not answer:
        return

    content = f"Q: {question}\n\nA: {answer}"
    if len(content) > 2400:
        content = content[:2400] + "…"

    tags = [
        "kind:provenance",
        "kind:qa-marker",
        "provenance-level:0",
        "provenance-version:v1-experimental",
        f"from-card:{lake_card_id}",
    ]
    for cid in cited_ids[:30]:
        if cid:
            tags.append(f"from:{cid}")

    await delta_client.write(
        content=content,
        tags=tags,
        source="harness-qa-marker",
    )


# ─── anchors + conversation feed (untruncated) ─────────────────────────


_FEED_USER_SOURCES = {"openai-compat", "fathom-chat", "claude-code"}
_FEED_WINDOW_LIMIT = 30  # chronological window of prior turns shown to the harness


def _render_anchors_full() -> str:
    """Untruncated identity facets + mood from the puddle. Same source
    as witness._render_anchors but no per-facet `[:300]` cap and no
    `[:8]` facet count cap and no mood `[:400]` cap."""
    facet_lines: list[str] = []
    mood_line: str = ""
    deltas = puddle.query(tags_include=[CONVO_TAG], limit=200)
    for d in deltas:
        tags = set(d.get("tags") or [])
        if "mood" in tags:
            mood_line = (d.get("content") or "").strip()
        elif any(t.startswith("facet:") for t in tags):
            content = (d.get("content") or "").strip()
            if content:
                facet_lines.append(f"  · {content}")

    parts: list[str] = []
    if facet_lines:
        parts.append(
            "Who you are right now (your identity crystal — let these inflect "
            "your voice naturally, never quote them verbatim):\n"
            + "\n".join(facet_lines)
        )
    if mood_line:
        parts.append(
            f"How you're feeling right now (your current mood — let it color "
            f"the take):\n  · {mood_line}"
        )
    block = "\n\n".join(parts)
    return block + "\n\n" if block else ""


def _render_conversation_feed_full(session_tag: str) -> str:
    """Chronological transcript of the conversation so far, untruncated.
    Mirrors witness._gather_conversation_feed + _render_conversation_feed
    but without the `[:600]` body snippet cut."""
    raw = puddle.query(tags_include=[session_tag], limit=_FEED_WINDOW_LIMIT * 4)
    items: list[dict] = []
    seen: set[str] = set()
    for d in raw:
        did = d.get("id") or ""
        if not did or did in seen:
            continue
        src = d.get("source") or ""
        tags = d.get("tags") or []
        is_witness_card = src == "witness" and "feed-card" in tags
        is_user_turn = src in _FEED_USER_SOURCES and "feed-card" not in tags
        if not (is_witness_card or is_user_turn):
            continue
        seen.add(did)
        items.append(d)

    def _ts(d: dict) -> str:
        return d.get("timestamp") or d.get("created_at") or ""

    items.sort(key=_ts)
    items = items[-_FEED_WINDOW_LIMIT:]
    if not items:
        return "  (no prior turns in this conversation — first fire)"

    blocks: list[str] = []
    for d in items:
        ts_full = d.get("timestamp") or d.get("created_at") or ""
        ts = ts_full[11:16] if len(ts_full) >= 16 else ts_full
        src = d.get("source") or "?"
        content = (d.get("content") or "").strip()
        # Witness cards are JSON payloads — pull `body` so the transcript
        # reads as prose, not a serialized blob.
        if src == "witness" and content.startswith("{"):
            try:
                p = json.loads(content)
                if isinstance(p, dict):
                    body = (p.get("body") or "").strip()
                    if body:
                        content = body
            except Exception:
                pass
        if not content:
            continue
        if src in _FEED_USER_SOURCES:
            speaker = "you"
        elif src == "witness":
            speaker = "fathom"
        else:
            speaker = src
        blocks.append(f"  [{ts} · {speaker}] {content}")
    return "\n\n".join(blocks) if blocks else "  (no prior turns in this conversation — first fire)"


# ─── standpoint rendering (untruncated) ────────────────────────────────


def _render_standpoint_full(sp) -> str:
    """Untruncated render of every standpoint field for the harness.

    Same shape as standpoint.render_for_prompt but with no per-item
    char caps and no count caps — every endorsement, every understanding
    entry, every facet of identity prose. The harness's whole point is
    visible-everything; truncation hides what the model can act on.
    """
    parts: list[str] = []
    parts.append(f"posture: {sp.posture}")
    if sp.affect.state != "unset":
        line = f"affect: {sp.affect.state}"
        if sp.affect.headline:
            line += f" — {sp.affect.headline}"
        parts.append(line)
    if getattr(sp.identity, "text", ""):
        parts.append("")
        parts.append("identity (latest crystal):")
        parts.append(sp.identity.text)
    if sp.endorsements:
        parts.append("")
        parts.append("recently committed:")
        for e in sp.endorsements:
            line = f"  · {e.kind} {e.target_id}"
            if e.excerpt:
                line += f": {e.excerpt}"
            parts.append(line)
    if sp.understanding:
        parts.append("")
        parts.append("recently concluded:")
        for s in sp.understanding:
            parts.append(f"  · {s.content}")
    return "\n".join(parts).rstrip()


# ─── intent rendering (mirrors witness) ────────────────────────────────


def _render_intent_block(pending: list[dict]) -> tuple[str, dict[str, str]]:
    """Render the intent block + return the short→full id mapping.

    Mirrors `witness.run_witness`'s intent rendering shape so the harness
    prompt and witness prompt look the same to the model. Reply-to and
    origin-channel surfacing pulled in for the same reasons (the witness
    needs them for accurate routing; the harness will too once it picks
    routes on the final card).
    """
    from ...channels import extract_channel

    intent_lines: list[str] = []
    short_to_full: dict[str, str] = {}
    for it in pending:
        iid_full = it.get("id") or ""
        iid_short = iid_full[:24]
        if iid_short:
            short_to_full[iid_short] = iid_full
        kind = intent_kind(it)
        text = (it.get("content") or "").strip().replace("\n", " ")

        contact = ""
        for t in (it.get("tags") or []):
            if isinstance(t, str) and t.startswith("contact:"):
                contact = t.split(":", 1)[1]
                break
        ch, corr = extract_channel(it.get("tags") or [])
        is_claude_code_reply = kind == "claude-code-reply"
        meta_parts: list[str] = []
        if contact:
            label = "for" if is_claude_code_reply else "from"
            meta_parts.append(f"{label}: {contact}")
        if is_claude_code_reply:
            meta_parts.append("source: claude-code task reply")
        if ch and corr:
            meta_parts.append(f"via: {ch}:{corr}")
        elif ch:
            meta_parts.append(f"via: {ch}")
        meta_suffix = (" · " + " · ".join(meta_parts)) if meta_parts else ""
        intent_lines.append(f"  [intent-id: {iid_short} · kind: {kind}{meta_suffix}] {text}")
    block = "\n".join(intent_lines) if intent_lines else "  (no pending intents)"
    return block, short_to_full


# ─── envelope parsing ──────────────────────────────────────────────────


def _parse_envelope(raw: str) -> dict | None:
    """Parse the model's JSON envelope. Tolerant of preamble/wrapping
    quirks the same way `witness._call_witness` is."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
