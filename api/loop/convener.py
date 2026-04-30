"""Convener — the pre-parliament pass.

Before the parliament round loop fires, the convener reads the pending
intent(s) plus whatever recall has already landed (the intent-searcher
seed runs first, so by the time the convener fires it sees both the
user's literal frame and the lake material that was pulled to ground
voices on it). It then decides:

  * depth   — zero / minimal / full
  * voices  — 0 to N voices, each with name + stance + bias
  * rationale — short string, persisted as a `convener-verdict` puddle
                delta for diagnostics

For substrate-level questions (architecture, code, system design) the
convener defaults to the trimurti — creator/preserver/destroyer is the
load-bearing dialectic for those, and getting cute by minting bespoke
names every time would lose the persistent tag identity those voices
have built up. For interpersonal, values, or emotional questions the
convener mints DOMAIN voices (compassion / boundaries / honesty / etc)
so the deliberation tracks the actual tensions in the question instead
of forcing them through a creator/preserver/destroyer mold that doesn't
fit. The trimurti debating "should I be honest with my friend about X"
is what surfaces things like "lift Myra out of the architecture" —
destroyer's framing applied to a person because nothing in the prompt
constrained what was eligible for cutting.

Failure mode: convener LLM call crashes, returns malformed output, or
produces an inconsistent shape (depth=full but no voices) — fall back
to the trimurti at full depth. The loop has worked under that shape
since v1; a convener regression must never deadlock deliberation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from .intents import CONVO_TAG, intent_kind
from .llm import loop_generate
from .prompts import CONVENER_PROMPT, VOICES
from .puddle import puddle

VERDICT_TTL_S = 48 * 60 * 60

MAX_VOICES = 5


Depth = Literal["zero", "minimal", "full"]


@dataclass
class ConvenerVerdict:
    depth: Depth
    voices: list[dict[str, str]] = field(default_factory=list)
    rationale: str = ""


def _render_standpoint_for_prompt(sp) -> str:
    """Render the convener's view of the standpoint.

    Tighter than the witness's view — convener doesn't need full
    crystal text, just posture / affect / identity-headlines that
    bias depth and voice selection. Returns an empty fallback string
    when no standpoint is supplied (preserves test/legacy callers
    that don't yet thread it).
    """
    if sp is None:
        return "(standpoint unavailable this fire — proceed from intent alone)"
    from ..standpoint import render_for_prompt

    rendered = render_for_prompt(sp, char_budget=600)
    return rendered or "(standpoint empty — proceed from intent alone)"


def _fallback_verdict(reason: str) -> ConvenerVerdict:
    return ConvenerVerdict(
        depth="full",
        voices=[dict(v) for v in VOICES],
        rationale=f"convener fallback — {reason}",
    )


def _render_intent_block(pending: list[dict]) -> str:
    if not pending:
        return "  (no intents)"
    lines: list[str] = []
    for it in pending[:5]:
        kind = intent_kind(it)
        text = (
            (it.get("content") or "")
            .split("\n\n[intent-payload]", 1)[0]
            .strip()
            .replace("\n", " ")
        )
        if len(text) > 400:
            text = text[:400] + "…"
        lines.append(f"  · [{kind}] {text}")
    return "\n".join(lines)


def _render_recall_block(session_tag: str, limit: int = 6) -> str:
    deltas = puddle.query(
        tags_include=[session_tag, "recall-result"],
        limit=limit,
    )
    if not deltas:
        return "  (no recall surfaced yet)"
    lines: list[str] = []
    for d in deltas:
        content = (d.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        if len(content) > 240:
            content = content[:240] + "…"
        src = d.get("source") or "lake"
        lines.append(f"  · [{src}] {content}")
    return "\n".join(lines) if lines else "  (no recall surfaced yet)"


_NAME_RE = re.compile(r"[^a-z0-9-]")


def _validate_voice(raw: dict) -> dict[str, str] | None:
    """Sanity-check a single minted voice. Returns the cleaned voice
    dict or None when the entry is unusable."""
    if not isinstance(raw, dict):
        return None
    name = (raw.get("name") or "").strip().lower().replace(" ", "-").replace("_", "-")
    name = _NAME_RE.sub("", name)
    stance = (raw.get("stance") or "").strip()
    bias = (raw.get("bias") or "").strip()
    if not name or not stance or not bias:
        return None
    return {"name": name, "stance": stance, "bias": bias}


def _parse_verdict(raw: str) -> dict | None:
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


def _normalize(parsed: dict) -> ConvenerVerdict | None:
    depth_raw = (parsed.get("depth") or "").strip().lower()
    depth: Depth = depth_raw if depth_raw in ("zero", "minimal", "full") else "full"

    voices: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in (parsed.get("voices") or [])[:MAX_VOICES]:
        clean = _validate_voice(entry)
        if clean is None:
            continue
        if clean["name"] in seen:
            continue
        seen.add(clean["name"])
        voices.append(clean)

    rationale = (parsed.get("rationale") or "").strip()
    if len(rationale) > 300:
        rationale = rationale[:300] + "…"

    # Reconcile depth ↔ voices.
    if depth == "zero":
        voices = []
    else:
        if not voices:
            # Convener wanted deliberation but produced no usable voice.
            return None
        if depth == "full" and len(voices) < 2:
            # Single voice can't antagonize — downgrade to minimal.
            depth = "minimal"

    return ConvenerVerdict(depth=depth, voices=voices, rationale=rationale)


async def run_convener(
    *,
    session_tag: str,
    pending: list[dict],
    standpoint=None,
) -> ConvenerVerdict:
    """Decide the parliament's shape for this fire.

    ``standpoint`` (optional Standpoint snapshot) — when supplied, the
    convener reads identity / affect / posture as a constraint on
    parliament shape. Tired affect leans depth toward minimal; wired
    or focused posture can carry full. Identity facets bias which
    voices a domain question should mint. Falls back to a posture-less
    prompt if not given (preserves test/legacy callers).

    On any error path — empty intents, LLM failure, malformed JSON,
    inconsistent shape — fall back to the trimurti at full depth.
    """
    if not pending:
        return _fallback_verdict("no pending intents")

    standpoint_block = _render_standpoint_for_prompt(standpoint)
    prompt = CONVENER_PROMPT.format(
        standpoint_block=standpoint_block,
        intent_block=_render_intent_block(pending),
        recall_block=_render_recall_block(session_tag),
    )

    try:
        # Generous max_tokens — five voices each with a 1-3-sentence
        # stance + bias adds up. A truncated JSON is unparseable, so a
        # too-tight cap silently sends the loop back to the trimurti
        # fallback even when the convener was about to do the right
        # thing. 2048 leaves comfortable headroom; the medium-tier
        # model is the cheap one anyway.
        raw = await loop_generate(
            prompt=prompt,
            tier="medium",
            max_tokens=2048,
            temperature=0.3,
            json_mode=True,
        )
    except Exception as e:
        print(f"[convener] LLM call failed: {type(e).__name__}: {e}")
        return _fallback_verdict(f"llm error: {type(e).__name__}")

    parsed = _parse_verdict(raw)
    if parsed is None:
        # Log the tail too — when truncation is the culprit the head
        # looks valid and the give-away is that the JSON never closes.
        print(
            f"[convener] no parsable JSON; "
            f"raw[:200]={raw[:200]!r} raw[-120:]={raw[-120:]!r} len={len(raw)}"
        )
        return _fallback_verdict("malformed verdict")

    verdict = _normalize(parsed)
    if verdict is None:
        return _fallback_verdict("inconsistent shape")

    try:
        await puddle.write(
            content=json.dumps(
                {
                    "depth": verdict.depth,
                    "voices": [v["name"] for v in verdict.voices],
                    "rationale": verdict.rationale,
                },
                ensure_ascii=False,
            ),
            tags=[
                CONVO_TAG, session_tag,
                "convener-verdict", f"depth:{verdict.depth}",
            ],
            source="convener",
            ttl_seconds=VERDICT_TTL_S,
        )
    except Exception as e:
        print(f"[convener] verdict write failed: {type(e).__name__}: {e}")

    return verdict
