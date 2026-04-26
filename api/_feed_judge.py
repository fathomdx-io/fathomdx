"""Judge stage — multi-axis scoring for synthesis candidates.

A synthesis pass produces a candidate card. Before writing it to the
lake, this module asks an independent LLM to rate the card on five
axes: salience, novelty, resonance, confidence, comfort. The judge
does NOT see the router, the per-pass budgets, the level thresholds,
or the calling pass's prompt. Architecturally separated so it cannot
calibrate toward "stay surfaced" — it only describes.

The router (api/_feed_router.py) consumes the judge's axes and decides
the level (or DROP).

On any failure (LLM unreachable, malformed output, parse error) the
judge returns safe-fallback scores tilted toward low confidence. The
router tags those out at INFO/DEBUG, which is the right move when we
literally don't know what the card is.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import delta_client
from .prompt import JUDGE_DIRECTIVE

log = logging.getLogger(__name__)
logging.getLogger(__name__).setLevel(logging.INFO)

AXES = ("salience", "novelty", "resonance", "confidence", "comfort")

# Returned when the judge can't run or its output can't be parsed. Mid
# salience/novelty/resonance/comfort but low confidence — the router
# will route these to a quiet level rather than the default surface.
_FALLBACK_AXES: dict[str, float] = {
    "salience": 0.50,
    "novelty": 0.50,
    "resonance": 0.50,
    "confidence": 0.20,
    "comfort": 0.50,
}


def _strip_fences(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _clamp_unit(v: Any) -> float:
    """Coerce a model-output value into [0.0, 1.0]. Anything unparseable
    becomes 0.5 (neutral) so a single bad axis doesn't poison the whole
    score; the caller can still see the rest of the dict."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _normalize_axes(parsed: dict) -> dict[str, float]:
    """Turn whatever the model returned into a canonical axes dict."""
    out: dict[str, float] = {}
    for axis in AXES:
        out[axis] = _clamp_unit(parsed.get(axis))
    return out


async def _recent_feed_cards_excerpt(contact_slug: str, limit: int = 10) -> str:
    """Compact list of recent card titles + body excerpts so the judge
    can rate novelty against actual recent content."""
    try:
        results = await delta_client.query(
            tags_include=["feed-card", f"contact:{contact_slug}"],
            limit=limit,
        )
    except Exception:
        return "(unavailable)"
    if not results:
        return "(no recent cards)"
    lines: list[str] = []
    for d in results:
        try:
            payload = json.loads(d.get("content") or "{}")
        except Exception:
            payload = {}
        title = (payload.get("title") or "").strip()
        body = (payload.get("body") or "").strip().replace("\n", " ")
        if len(body) > 160:
            body = body[:160] + "…"
        if title:
            lines.append(f"  • {title}")
            if body:
                lines.append(f"      {body}")
    return "\n".join(lines) if lines else "(no recent cards)"


async def _recent_engagement_excerpt(contact_slug: str, limit: int = 20) -> str:
    """Compact list of the user's recent engagement signals so the
    judge can rate resonance against actual reactions."""
    try:
        results = await delta_client.query(
            tags_include=["feed-engagement", f"contact:{contact_slug}"],
            limit=limit,
        )
    except Exception:
        return "(unavailable)"
    if not results:
        return "(no engagement yet)"
    lines: list[str] = []
    for d in results:
        try:
            payload = json.loads(d.get("content") or "{}")
        except Exception:
            payload = {}
        kind = payload.get("kind") or "?"
        topic = payload.get("topic") or ""
        excerpt = (payload.get("card_excerpt") or "")[:100]
        topic_str = f"[{topic}] " if topic else ""
        lines.append(f"  {kind:>10} · {topic_str}{excerpt}")
    return "\n".join(lines)


def _format_card(payload: dict) -> str:
    """Render the candidate card into a compact prompt block."""
    bits: list[str] = []
    if payload.get("kicker"):
        bits.append(f"kicker: {payload['kicker']}")
    if payload.get("title"):
        bits.append(f"title:  {payload['title']}")
    if payload.get("body"):
        bits.append(f"body:   {payload['body']}")
    if payload.get("tail"):
        bits.append(f"tail:   {payload['tail']}")
    if payload.get("link"):
        bits.append(f"link:   {payload['link']}")
    if payload.get("body_image"):
        bits.append(f"image:  {payload['body_image']}")
    return "\n".join(bits) if bits else "(empty card)"


async def judge(
    payload: dict,
    contact_slug: str,
    kind: str,
) -> dict[str, float]:
    """Score a candidate card on the five judge axes.

    Returns a dict with keys salience/novelty/resonance/confidence/comfort,
    each in [0.0, 1.0]. On any failure path returns the fallback scores
    (low confidence) — the router treats those gracefully.
    """
    # Lazy import to avoid the api↔llm_config bootstrap circle that some
    # call-sites hit during cold start.
    try:
        from . import llm_config

        client, model = await llm_config.resolve_tier("medium")
    except Exception:
        log.exception("feed_judge: tier resolution failed; using fallback axes")
        return dict(_FALLBACK_AXES)

    recent_cards = await _recent_feed_cards_excerpt(contact_slug)
    recent_engagement = await _recent_engagement_excerpt(contact_slug)

    user_message = (
        f"=== CANDIDATE CARD ===\nkind: {kind}\n{_format_card(payload)}\n\n"
        f"=== RECENT FEED CARDS (for novelty rating) ===\n{recent_cards}\n\n"
        f"=== RECENT ENGAGEMENT (for resonance rating) ===\n{recent_engagement}\n\n"
        "Rate the candidate on the five axes. JSON only."
    )

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": JUDGE_DIRECTIVE},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
        )
    except Exception:
        log.exception("feed_judge: LLM call failed; using fallback axes")
        return dict(_FALLBACK_AXES)

    text = resp.choices[0].message.content if resp.choices else ""
    raw = _strip_fences(text or "")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("not a JSON object")
    except Exception:
        log.warning("feed_judge: non-JSON output; using fallback. excerpt: %r", (text or "")[:200])
        return dict(_FALLBACK_AXES)

    return _normalize_axes(parsed)
