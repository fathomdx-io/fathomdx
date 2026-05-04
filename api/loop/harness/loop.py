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
import os
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo


# Local timezone for the NOW block. UTC reads sterile and confuses the
# model when humans say "this morning" — anchor to the operator's wall
# clock instead. Override with FATHOM_LOCAL_TZ if Fathom moves.
_LOCAL_TZ_NAME = os.environ.get("FATHOM_LOCAL_TZ", "America/Chicago")
try:
    _LOCAL_TZ = ZoneInfo(_LOCAL_TZ_NAME)
except Exception:
    _LOCAL_TZ = ZoneInfo("UTC")


def _render_now_block() -> str:
    """Current local timestamp + human-readable orientation, regenerated
    every turn so the model has a fresh anchor for "now."

    Format gives the local wall clock first (what Myra means when she
    says "morning") followed by the UTC equivalent in parentheses so
    Fathom can still cross-reference the lake's ISO-Z timestamps when
    needed.

    Without this anchor, Fathom drifts: the prompt is full of older
    deltas with their own timestamps, and there's nothing telling the
    model which moment is the present.
    """
    utc = datetime.now(timezone.utc)
    local = utc.astimezone(_LOCAL_TZ)
    if 5 <= local.hour < 12:
        part = "morning"
    elif 12 <= local.hour < 18:
        part = "afternoon"
    elif 18 <= local.hour < 22:
        part = "evening"
    else:
        part = "night"
    return (
        f"{local.strftime('%Y-%m-%d %H:%M')} {local.tzname()} "
        f"({local.strftime('%A')} {part}) · "
        f"UTC {utc.strftime('%H:%M')}"
    )

from .. import witness as witness_mod
from ..intents import CONVO_TAG, intent_kind
from ..llm import loop_generate
from ..puddle import puddle
from .prompts import (
    HARNESS_SYSTEM,
    INTROSPECTION_SYSTEM,
    REVIEW_SYSTEM,
    render_tool_history,
)
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
    disabled_tools: set[str] | None = None,
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
    # Provenance consolidation lives in the post-response review pass —
    # disable it in the main loop so the model doesn't compete between
    # answering and consolidating. Caller may add more disables (e.g.
    # introspect() child fires disable nested introspect for depth-1).
    main_loop_disabled = {"propose_provenance"}
    if disabled_tools:
        main_loop_disabled = main_loop_disabled | disabled_tools
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
    # Active plan state — populated when the model calls plan(question);
    # rendered into the prompt with progress markers each subsequent
    # turn. Empty list means "no plan set, free-form mode."
    current_plan: list[dict] = []

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
            now_block=_render_now_block(),
            standpoint_block=standpoint_block,
            anchors_block=anchors_block,
            feed_block=feed_block,
            intent_block=intent_block,
            hosts_block=hosts_block,
            routines_block=routines_block,
            plan_block=_render_plan_block(current_plan),
            tool_history=render_tool_history(tool_history),
            turn_number=turn,
            max_turns=MAX_TURNS,
        )

        await _emit(cb, "turn_begin", {
            "turn": turn,
            "prompt_chars": len(prompt),
            "prompt": prompt,  # full text so the page can show what was sent
        })

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
                # Lean shape: {kind: "respond", body: "..."} carries body
                # at top level. Full shape: cards: [...]. Forward both
                # so the page can render whichever the model emitted.
                "cards": parsed.get("cards") or [],
                "body": parsed.get("body") or "",
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
            # The model may declare which plan step this call advances.
            # Used to flip ○ → ⟳ on dispatch and ⟳ → ✓ on success. Plain
            # int or string-int both accepted.
            plan_step_raw = parsed.get("plan_step")
            plan_step = _coerce_plan_step(plan_step_raw, current_plan)
            if thinking:
                print(f"[harness] turn {turn}: {tool_name} — {thinking[:120]}")
            else:
                print(f"[harness] turn {turn}: {tool_name}")
            await _emit(cb, "tool_call", {
                "turn": turn, "tool": tool_name, "args": args_raw,
                "thinking": thinking, "plan_step": plan_step,
            })
            if plan_step is not None:
                _set_plan_status(current_plan, plan_step, "in_progress")
                await _emit(cb, "plan_step_status", {
                    "step": plan_step, "status": "in_progress",
                })
            entry = await _dispatch_tool(
                turn=turn,
                tool_name=tool_name,
                args=args_raw,
                session_tag=session_tag,
                pending=pending,
                standpoint=standpoint,
                disabled_tools=main_loop_disabled,
            )
            tool_history.append(entry)
            # Plan tool special handling — parse the steps payload and
            # update current_plan. Replaces any existing plan (revisions
            # are deliberate moves; the prior plan's progress is reset).
            if tool_name == "plan" and not entry.get("error"):
                new_plan = _parse_plan_payload(entry.get("result") or "")
                if new_plan:
                    revision = bool(current_plan)
                    current_plan = new_plan
                    await _emit(cb, "plan_revised" if revision else "plan_set", {
                        "steps": current_plan,
                    })
            await _emit(cb, "tool_result", {
                "turn": turn, "tool": tool_name,
                "result": entry.get("result"),
                "error": entry.get("error"),
                "plan_step": plan_step,
            })
            # Mark plan step done after tool result, regardless of error
            # (the model can re-step or revise; we don't gate on success).
            if plan_step is not None and not entry.get("error"):
                _set_plan_status(current_plan, plan_step, "done")
                await _emit(cb, "plan_step_status", {
                    "step": plan_step, "status": "done",
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

    # Post-response review pass — separate turn whose only job is to
    # consolidate. Looks at what was recalled + what was said, decides
    # whether a coherent stretch deserves a name. Soft-fails so a
    # broken review never blocks the addressed-intents return.
    try:
        await _run_post_response_review(
            cb=cb,
            session_tag=session_tag,
            pending=pending,
            tool_history=tool_history,
            final_response=final_response,
        )
    except Exception as e:
        print(f"[harness] post-response review crashed: {type(e).__name__}: {e}")
        await _emit(cb, "review_error", {"type": type(e).__name__, "message": str(e)})

    await _emit(cb, "done", {"addressed": addressed})
    return addressed


# ─── introspection mode ────────────────────────────────────────────────


INTROSPECTION_MAX_TURNS = 8


async def run_introspection(
    *,
    focus: str,
    session_tag: str,
    event_callback: EventCallback | None = None,
) -> dict:
    """Run an introspection fire — Fathom sitting with itself.

    No user intent, no card to dispatch. The model walks its own
    substrate via the same harness tools and writes a reflection
    delta when it's seen enough.

    `focus` is what to sit with — a string like "the navier-stokes
    work this past week" or "my mood drift since the harness
    refactor." For Phase 1 the caller supplies it explicitly. For
    Phase 2 a focus pre-pass picks it from substrate signals.

    Returns a small dict with the outcome:
      {"kind": "reflect", "delta_id": "...", "shape": "...", "from_count": N}
      {"kind": "skip", "reason": "..."}
      {"kind": "max_turns"}
      {"kind": "error", "type": "...", "message": "..."}

    The reflection itself lands as `kind:reflection` in the lake with
    `from:<id>` provenance pointers — same engagement-graph machinery
    as any other delta.
    """
    from ... import standpoint as standpoint_mod
    from ... import delta_client

    cb = event_callback
    focus = (focus or "").strip()
    if not focus:
        return {"kind": "error", "type": "ValueError",
                "message": "focus is required"}

    await _emit(cb, "introspection_start", {
        "session_tag": session_tag,
        "focus": focus,
        "max_turns": INTROSPECTION_MAX_TURNS,
    })

    try:
        standpoint = await standpoint_mod.current(session_tag=session_tag)
    except Exception as e:
        print(f"[introspection] standpoint fetch failed: {type(e).__name__}: {e}")
        standpoint = None

    standpoint_block = (
        _render_standpoint_full(standpoint) if standpoint
        else "(standpoint unavailable — speak from anchors only)"
    )
    focus_block = focus

    tool_history: list[dict] = []
    final_reflection: dict | None = None

    for turn in range(1, INTROSPECTION_MAX_TURNS + 1):
        anchors_block = _render_anchors_full()
        prompt = INTROSPECTION_SYSTEM.format(
            now_block=_render_now_block(),
            focus_block=focus_block,
            standpoint_block=standpoint_block,
            anchors_block=anchors_block,
            tool_history=render_tool_history(tool_history),
            turn_number=turn,
            max_turns=INTROSPECTION_MAX_TURNS,
        )
        await _emit(cb, "introspection_turn_begin", {
            "turn": turn, "prompt_chars": len(prompt), "prompt": prompt,
        })

        try:
            raw = await loop_generate(
                prompt=prompt,
                tier="hard",
                max_tokens=MAX_TOKENS_PER_TURN,
                temperature=0.7,
                json_mode=True,
            )
        except Exception as e:
            print(f"[introspection] LLM call crashed turn {turn}: "
                  f"{type(e).__name__}: {e}")
            await _emit(cb, "error", {"where": "llm_call", "turn": turn,
                                      "type": type(e).__name__, "message": str(e)})
            return {"kind": "error", "type": type(e).__name__, "message": str(e)}

        parsed = _parse_envelope(raw)
        if parsed is None:
            tool_history.append({
                "turn": turn, "tool": "(parse-error)", "args": {},
                "error": "envelope unparseable",
            })
            await _emit(cb, "parse_error", {
                "turn": turn, "raw_head": raw[:200], "raw_tail": raw[-120:],
            })
            continue

        kind = (parsed.get("kind") or "").strip()

        if kind == "reflect":
            # Hard rule: refuse to settle without at least one successful
            # tool call. Without this, the model can paraphrase the
            # standpoint block (which carries recently-committed deltas,
            # mood, identity) and call it a reflection — sounds plausible
            # but isn't actually a substrate walk. Force at least one
            # turn of looking before allowing settle.
            successful_tool_calls = sum(
                1 for h in tool_history
                if h.get("tool") and h.get("tool") not in ("(parse-error)", "(unknown-kind)")
                and not h.get("error")
            )
            if successful_tool_calls == 0:
                tool_history.append({
                    "turn": turn, "tool": "(early-reflect)", "args": {},
                    "error": "rejected — must walk the substrate before settling. "
                             "Call a tool (semantic / time / pattern / state / relate / "
                             "expand / ascend / deliberate) on yourself first.",
                })
                await _emit(cb, "introspection_early_reflect_rejected", {
                    "turn": turn,
                })
                continue
            final_reflection = parsed
            await _emit(cb, "introspection_reflect", {
                "turn": turn,
                "body": parsed.get("body") or "",
                "from_ids": parsed.get("from_ids") or [],
                "shape": parsed.get("shape") or "",
            })
            break

        if kind == "skip":
            reason = (parsed.get("reason") or "").strip() or "(no reason)"
            await _emit(cb, "introspection_skipped", {
                "turn": turn, "reason": reason,
            })
            return {"kind": "skip", "reason": reason}

        if kind == "tool_call":
            tool_name = (parsed.get("tool") or "").strip()
            args_raw = parsed.get("args") or {}
            thinking = (parsed.get("thinking") or "").strip()
            await _emit(cb, "tool_call", {
                "turn": turn, "tool": tool_name, "args": args_raw,
                "thinking": thinking,
            })
            entry = await _dispatch_tool(
                turn=turn,
                tool_name=tool_name,
                args=args_raw,
                session_tag=session_tag,
                pending=[],
                standpoint=standpoint,
            )
            tool_history.append(entry)
            await _emit(cb, "tool_result", {
                "turn": turn, "tool": tool_name,
                "result": entry.get("result"),
                "error": entry.get("error"),
            })
            continue

        # Unknown envelope kind — log and consume the turn.
        tool_history.append({
            "turn": turn, "tool": "(unknown-kind)", "args": {},
            "error": f"unknown envelope kind: {kind!r}",
        })
        await _emit(cb, "unknown_kind", {"turn": turn, "kind": kind, "raw": parsed})

    if final_reflection is None:
        await _emit(cb, "introspection_max_turns", {
            "max_turns": INTROSPECTION_MAX_TURNS,
        })
        return {"kind": "max_turns"}

    body = (final_reflection.get("body") or "").strip()
    if not body:
        await _emit(cb, "introspection_skipped", {
            "turn": turn, "reason": "reflect envelope had empty body",
        })
        return {"kind": "skip", "reason": "empty body"}

    raw_from = final_reflection.get("from_ids") or []
    from_ids: list[str] = []
    seen: set[str] = set()
    for fid in raw_from:
        if not isinstance(fid, str):
            continue
        s = fid.strip()
        if len(s) == 12 and all(c in "0123456789abcdef" for c in s) and s not in seen:
            seen.add(s)
            from_ids.append(s)

    shape = (final_reflection.get("shape") or "").strip()[:80]
    shape_slug = "".join(
        c if c.isalnum() else "-" for c in shape.lower()
    ).strip("-")[:80] or "unshaped"

    tags = [
        "kind:reflection",
        "produced-by:harness-introspection",
        f"shape:{shape_slug}",
    ]
    for fid in from_ids[:60]:
        tags.append(f"from:{fid}")

    content = body
    if len(content) > 6000:
        content = content[:6000] + "…"

    try:
        written = await delta_client.write(
            content=content,
            tags=tags,
            source="harness-introspection",
        )
    except Exception as e:
        print(f"[introspection] reflection write failed: {type(e).__name__}: {e}")
        await _emit(cb, "error", {"where": "reflection_write",
                                  "type": type(e).__name__, "message": str(e)})
        return {"kind": "error", "type": type(e).__name__, "message": str(e)}

    delta_id = (written or {}).get("id") or ""
    await _emit(cb, "introspection_recorded", {
        "delta_id": delta_id,
        "shape": shape,
        "from_count": len(from_ids),
    })
    return {
        "kind": "reflect",
        "delta_id": delta_id,
        "shape": shape,
        "from_count": len(from_ids),
    }


# ─── dialogue mode ─────────────────────────────────────────────────────


async def run_dialogue(
    *,
    seed: str,
    session_tag: str,
    event_callback: EventCallback | None = None,
    max_rounds: int = 4,
) -> dict:
    """Self-dialogue — fire `run_harness` in a loop, feeding each round's
    response back as the next round's prompt.

    No new prompts, no voice gymnastics. The harness already does
    Q → A; dialogue just makes the next Q be the prior A. The
    conversation emerges because each call genuinely deliberates over
    fresh substrate.

    Each round:
      1. Seed an intent in the puddle with the current message.
      2. Fire `run_harness` against it; capture the response body
         from the `respond` event.
      3. If a body comes back, use it as the next round's input.
         Otherwise stop.

    `max_rounds` caps the conversation. There's no settle detection —
    by design. If the dialogue feels too long, the operator interrupts.
    """
    from ..intents import CONVO_TAG
    from ..puddle import puddle as _puddle

    cb = event_callback
    seed = (seed or "").strip()
    if not seed:
        return {"kind": "error", "type": "ValueError",
                "message": "seed is required"}

    await _emit(cb, "dialogue_start", {
        "session_tag": session_tag, "seed": seed, "max_rounds": max_rounds,
    })

    transcript: list[dict] = []
    current_message = seed

    for round_num in range(1, max_rounds + 1):
        await _emit(cb, "dialogue_round_begin", {
            "round": round_num, "max_rounds": max_rounds,
            "input": current_message,
        })

        # Captured response body for this round; populated when the
        # wrapped callback sees the harness's `respond` event.
        captured: dict[str, str] = {"body": ""}

        async def round_cb(event: str, payload: dict) -> None:
            if event == "respond":
                cards = payload.get("cards") or []
                if cards and isinstance(cards[0], dict):
                    captured["body"] = (cards[0].get("body") or "").strip()
                elif payload.get("body"):
                    captured["body"] = (payload.get("body") or "").strip()
            # Tag every wrapped event with the round so the UI can
            # scope activity per round if it wants to.
            if cb is not None:
                await _emit(cb, event, {"round": round_num, **payload})

        # Seed the intent. The harness needs a pending intent to fire.
        try:
            intent = await _puddle.write(
                content=current_message,
                tags=[
                    CONVO_TAG, session_tag, "intent", "kind:question",
                    "harness-dialogue",
                ],
                source="harness-dialogue-self",
                ttl_seconds=60 * 60,
            )
        except Exception as e:
            await _emit(cb, "dialogue_error", {
                "round": round_num, "where": "seed_intent",
                "type": type(e).__name__, "message": str(e),
            })
            break

        try:
            await run_harness(
                session_tag=session_tag,
                pending=[intent],
                event_callback=round_cb,
            )
        except Exception as e:
            await _emit(cb, "dialogue_error", {
                "round": round_num, "where": "run_harness",
                "type": type(e).__name__, "message": str(e),
            })
            break

        body = captured["body"]
        if not body:
            await _emit(cb, "dialogue_round_skipped", {
                "round": round_num,
                "reason": "harness produced no response body",
            })
            break

        transcript.append({
            "round": round_num,
            "input": current_message,
            "response": body,
        })
        await _emit(cb, "dialogue_round_end", {
            "round": round_num, "input": current_message, "response": body,
        })

        current_message = body

    await _emit(cb, "dialogue_done", {
        "rounds": len(transcript),
        "transcript": transcript,
    })
    return {
        "kind": "dialogue",
        "rounds": len(transcript),
        "transcript": transcript,
    }


# ─── plan helpers ──────────────────────────────────────────────────────


_PLAN_GLYPH = {"pending": "○", "in_progress": "⟳", "done": "✓"}


def _render_plan_block(current_plan: list[dict]) -> str:
    """Format the active plan for inclusion in the turn prompt.

    Empty when no plan is set — the model is in free-form mode and the
    block disappears entirely. When a plan exists, render each step
    with its progress glyph + purpose + hint, and remind the model to
    declare `plan_step:<n>` on tool calls.
    """
    if not current_plan:
        return ""
    lines = ["══ ACTIVE PLAN ══"]
    for step in current_plan:
        glyph = _PLAN_GLYPH.get(step.get("status", "pending"), "○")
        purpose = step.get("purpose", "")
        hint = step.get("hint", "")
        suffix = f" — {hint}" if hint else ""
        lines.append(f"  {glyph} {step.get('step', '?')}. {purpose}{suffix}")
    lines.append(
        "\nWork through the pending steps. On each tool call, include "
        '`"plan_step": <n>` so progress reflects in the trace. Call '
        "`plan(question)` again only if a result genuinely invalidates "
        "the plan — revising too freely defeats the point."
    )
    return "\n".join(lines) + "\n\n"


def _coerce_plan_step(raw, plan: list[dict]) -> int | None:
    """Pull a plan-step number off a tool_call envelope. Tolerates int
    or string-int. Returns None when no plan exists, the value isn't a
    number, or the step is out of range."""
    if not plan or raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < 1 or n > len(plan):
        return None
    return n


def _set_plan_status(plan: list[dict], step: int, status: str) -> None:
    """Mark a plan step's status. Mutates plan in place."""
    for s in plan:
        if s.get("step") == step:
            s["status"] = status
            return


def _parse_plan_payload(result: str) -> list[dict]:
    """Parse the JSON the plan tool returned and produce a fresh plan
    list ready to render. Tolerant of both well-formed JSON and JSON
    inside an error wrapper.

    Each step is normalized to:
      {"step": <n>, "purpose": "...", "hint": "...", "status": "pending"}
    """
    if not result:
        return []
    try:
        parsed = json.loads(result)
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []
    steps_raw = parsed.get("steps")
    if not isinstance(steps_raw, list):
        return []
    plan: list[dict] = []
    for i, s in enumerate(steps_raw, start=1):
        if not isinstance(s, dict):
            continue
        purpose = (s.get("purpose") or "").strip()
        hint = (s.get("hint") or "").strip()
        if not purpose:
            continue
        plan.append({
            "step": i,
            "purpose": purpose,
            "hint": hint,
            "status": "pending",
        })
    return plan


# ─── post-response review ──────────────────────────────────────────────


async def _run_post_response_review(
    *,
    cb: EventCallback | None,
    session_tag: str,
    pending: list[dict],
    tool_history: list[dict],
    final_response: dict,
) -> None:
    """Fire one focused turn whose only job is to decide whether the
    fire's working set deserves a provenance proposal.

    The main loop is for answering. This pass is for consolidating.
    Splitting them means the model isn't choosing between competing
    objectives in one turn — the answer gets full attention, then the
    review gets a stripped prompt with one job.

    Only outcome paths:
      · `tool_call` → propose_provenance (auto-approves at L1/L2)
      · `skip`      → no-op, with a one-sentence reason logged

    Anything else is treated as skip.
    """
    if not pending:
        return

    question = (pending[0].get("content") or "").strip()
    question = question.split("\n\n[intent-payload]", 1)[0].strip()
    if not question:
        return

    cards = final_response.get("cards") or []
    if cards:
        answer = "\n\n".join(
            (c.get("body") or "").strip()
            for c in cards
            if isinstance(c, dict) and (c.get("body") or "").strip()
        )
    else:
        answer = (final_response.get("body") or "").strip()
    if not answer:
        return

    # Skip if nothing was actually pulled — pure-standpoint fires have
    # no working set to consolidate over.
    real_tool_calls = [
        h for h in tool_history
        if h.get("tool") and h.get("tool") not in ("(parse-error)", "(unknown-kind)")
        and not h.get("error")
    ]
    if not real_tool_calls:
        await _emit(cb, "review_skipped", {"reason": "no successful tool calls — nothing to consolidate"})
        return

    prompt = REVIEW_SYSTEM.format(
        question=question,
        answer=answer,
        tool_history=render_tool_history(tool_history),
    )

    await _emit(cb, "review_begin", {"prompt_chars": len(prompt)})

    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="hard",
            max_tokens=MAX_TOKENS_PER_TURN,
            temperature=0.6,
            json_mode=True,
        )
    except Exception as e:
        await _emit(cb, "review_error", {"where": "llm_call",
                                         "type": type(e).__name__, "message": str(e)})
        return

    parsed = _parse_envelope(raw)
    if parsed is None:
        await _emit(cb, "review_skipped", {"reason": "review envelope unparseable"})
        return

    kind = (parsed.get("kind") or "").strip()
    if kind == "skip":
        reason = (parsed.get("reason") or "").strip() or "(no reason given)"
        print(f"[harness] review: skip — {reason[:200]}")
        await _emit(cb, "review_skipped", {"reason": reason})
        return

    if kind != "tool_call":
        await _emit(cb, "review_skipped", {"reason": f"unknown review kind: {kind!r}"})
        return

    tool_name = (parsed.get("tool") or "").strip()
    if tool_name != "propose_provenance":
        await _emit(cb, "review_skipped", {"reason": f"review may only call propose_provenance, got {tool_name!r}"})
        return

    args_raw = parsed.get("args") or {}
    thinking = (parsed.get("thinking") or "").strip()
    print(f"[harness] review: propose_provenance — {thinking[:120]}")
    await _emit(cb, "review_proposing", {"tool": tool_name, "args": args_raw, "thinking": thinking})

    entry = await _dispatch_tool(
        turn=0,
        tool_name=tool_name,
        args=args_raw,
        session_tag=session_tag,
        pending=pending,
        standpoint=None,
        disabled_tools=set(),  # review pass: propose_provenance allowed
    )
    await _emit(cb, "review_result", {
        "tool": tool_name,
        "result": entry.get("result"),
        "error": entry.get("error"),
    })


# ─── tool dispatch ─────────────────────────────────────────────────────


async def _dispatch_tool(
    *,
    turn: int,
    tool_name: str,
    args: dict,
    session_tag: str,
    pending: list[dict],
    standpoint,
    disabled_tools: set[str] | None = None,
) -> dict:
    """Run one tool call. Returns the history entry to append.

    `disabled_tools` is the set of tool names this fire isn't allowed to
    call. Used to:
      · suppress `propose_provenance` in the main loop (it runs in the
        post-response review pass instead)
      · suppress `introspect` in child fires spawned by an outer
        introspect() call (depth-1 recursion cap)

    Default `None` is treated as `{"propose_provenance"}` — the safe
    default since provenance writes are consolidation, not answering.
    Pass an explicit empty set or different set to override.
    """
    if disabled_tools is None:
        disabled_tools = {"propose_provenance"}
    if tool_name in disabled_tools:
        return {
            "turn": turn, "tool": tool_name, "args": args,
            "error": f"{tool_name!r} is not available in this fire",
        }
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
    elif tool_name == "propose_provenance":
        cleaned["session_tag"] = session_tag
    elif tool_name == "introspect":
        cleaned["session_tag"] = session_tag

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

    The response payload accepts two shapes:

      Lean (chat-reply only — the high-frequency case):
        {"kind": "respond", "body": "..."}
      The harness fills in route="chat-reply", addresses=all pending
      intent ids, kicker/title/tail empty. No need for the model to
      pad out a feed-card form when the answer is just conversation.

      Full (any route — feed-card / claude-code dispatch / tool
      proposal / multi-card fire):
        {"kind": "respond", "cards": [...], "attestation": "...", ...}
    """
    cards_raw = response.get("cards")
    available_hosts = await witness_mod._available_claude_code_hosts()
    primary_intent = (pending[0].get("content") or "").strip() if pending else ""

    # Lean chat-reply path: a top-level `body` with no `cards` array.
    # Synthesize a single chat-reply card from it.
    if not cards_raw:
        body = (response.get("body") or "").strip()
        if body:
            cards_raw = [{"route": "chat-reply", "body": body}]

    cards: list[dict] = []
    for card in (cards_raw or []):
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

    # Centroid of cited deltas — same two-embedding shape as L1+
    # provenance, just degenerate (one-cite markers have a centroid
    # equal to the single delta's embedding). Skipped if compute
    # fails — the marker still lands with summary-only retrieval.
    from ... import provenance_centroid
    centroid = await provenance_centroid.compute_centroid(cited_ids)

    await delta_client.write(
        content=content,
        tags=tags,
        source="harness-qa-marker",
        provenance_embedding=centroid,
    )


# ─── anchors + conversation feed (untruncated) ─────────────────────────


_FEED_USER_SOURCES = {"openai-compat", "fathom-chat", "claude-code", "harness-test"}
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
    quirks the same way `witness._call_witness` is.

    Sometimes Gemini returns the envelope wrapped in a list (e.g.
    `[{"kind": "tool_call", ...}]`). Unwrap a single-item list of
    dicts. Return None for any other shape — including a list of
    multiple envelopes, which would require the model to slow down
    and emit one envelope per turn.
    """
    if not raw:
        return None

    def _unwrap(v):
        # Accept a dict directly. If we get a list, take the first dict
        # (Gemini sometimes returns multiple envelopes per response —
        # we honor the first and ignore the rest, which forces the
        # model to slow down to one envelope per turn next time).
        if isinstance(v, dict):
            return v
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    return item
        return None

    try:
        return _unwrap(json.loads(raw))
    except Exception:
        pass
    # Fall back to extracting the first JSON-shaped object from prose
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return _unwrap(json.loads(m.group(0)))
    except Exception:
        return None
