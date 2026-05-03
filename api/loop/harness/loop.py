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

import json
import re

from .. import witness as witness_mod
from ..intents import intent_kind
from ..llm import loop_generate
from ..puddle import puddle
from .prompts import HARNESS_SYSTEM, render_tool_history
from .tools import TOOL_HANDLERS, TOOL_MODEL_ARGS


MAX_TURNS = 8                # hard cap on tool calls per fire
MAX_TOKENS_PER_TURN = 4096   # response budget — final card needs room


# ─── entry point ───────────────────────────────────────────────────────


async def run_harness(
    *,
    session_tag: str,
    pending: list[dict],
    voice_order: list[str] | None = None,  # back-compat: accepted but unused
    standpoint=None,
) -> list[str]:
    """Run one full harness fire — drop-in replacement for `run_witness`.

    Returns the list of intent-ids the harness claims to have addressed,
    same return shape as `witness.run_witness`. `voice_order` is accepted
    for signature compatibility but ignored — the harness elects its own
    deliberation via the `deliberate` tool, so an outer parliament order
    isn't meaningful here.
    """
    if not pending:
        return []

    # Pre-render the persistent context blocks that don't change across
    # tool-call turns within this fire. Re-rendered once per turn so
    # telepathy refreshes (anchors) or new conversation feed entries
    # land mid-loop, but cheap enough that we don't cache.
    intent_block, short_to_full = _render_intent_block(pending)
    available_hosts = await witness_mod._available_claude_code_hosts()
    hosts_block = witness_mod._render_hosts_block(available_hosts)
    routines_block = await witness_mod._render_routines_block(available_hosts)
    standpoint_block = witness_mod._render_standpoint_for_witness(standpoint) or (
        "(standpoint unavailable — speak from anchors and feed)"
    )

    tool_history: list[dict] = []
    final_response: dict | None = None

    for turn in range(1, MAX_TURNS + 1):
        # Re-render anchors + feed each turn so telepathy refreshes
        # (which run on a slow clock) land if they happen mid-fire.
        anchors_block = witness_mod._render_anchors()
        feed_items = witness_mod._gather_conversation_feed(session_tag=session_tag)
        feed_block = witness_mod._render_conversation_feed(feed_items)

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
            continue

        kind = (parsed.get("kind") or "").strip()

        if kind == "respond":
            final_response = parsed
            print(f"[harness] turn {turn}: RESPOND ({len(parsed.get('cards') or [])} cards)")
            break

        if kind == "tool_call":
            tool_name = (parsed.get("tool") or "").strip()
            args_raw = parsed.get("args") or {}
            thinking = (parsed.get("thinking") or "").strip()
            if thinking:
                print(f"[harness] turn {turn}: {tool_name} — {thinking[:120]}")
            else:
                print(f"[harness] turn {turn}: {tool_name}")
            entry = await _dispatch_tool(
                turn=turn,
                tool_name=tool_name,
                args=args_raw,
                session_tag=session_tag,
                pending=pending,
                standpoint=standpoint,
            )
            tool_history.append(entry)
            continue

        # Unknown kind — log and consume the turn.
        print(f"[harness] turn {turn} unknown kind {kind!r}")
        tool_history.append({
            "turn": turn, "tool": "(unknown-kind)", "args": {},
            "error": f"unknown envelope kind: {kind!r}",
        })

    if final_response is None:
        print(f"[harness] hit max turns ({MAX_TURNS}) without responding — silent fire")
        return []

    return await _dispatch_response(
        response=final_response,
        pending=pending,
        short_to_full=short_to_full,
        session_tag=session_tag,
        voice_order=voice_order,
    )


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

    if first_lake_id:
        try:
            await witness_mod._write_constituting_writes(
                lake_card_id=first_lake_id,
                attestation=(response.get("attestation") or "").strip(),
                mood_shift=witness_mod._parse_mood_shift(response.get("mood_shift")),
                cited_ids=witness_mod._clean_id_list(response.get("cited_ids")),
                dropped_ids=witness_mod._clean_id_list(response.get("dropped_ids")),
            )
        except Exception as e:
            print(f"[harness] constituting-act writes failed: {type(e).__name__}: {e}")

    return full_addressed_union


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
        if len(text) > 280:
            text = text[:280] + "…"

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
