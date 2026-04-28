"""Feed-orient crystal regeneration — the heavy aggregation layer.

The witness reads identity facets and mood from the puddle on every
fire to anchor its take. The feed-orient crystal is a third anchor:
"what does the user actually want to see right now," distilled from
their accumulated engagement (the +/- markers, the cards they opened
into chat, what they pushed back on).

This module mirrors auto_regen.py's pattern for the identity crystal,
adapted for engagement-driven cadence:

  * Background task polls every POLL_INTERVAL_S.
  * Counts feed-engagement deltas written since the last
    crystal:feed-orient delta (or all of them, on first run).
  * If count >= MIN_ENGAGEMENTS and we're past the cooldown, fire a
    regen pass: gather inputs, run FEED_CRYSTAL_DIRECTIVE through
    loop_generate, write the result as a crystal:feed-orient lake
    delta. Telepathy picks it up on its next tick and surfaces it to
    the witness as a `facet:feed-orient` puddle delta alongside the
    identity facets.

The output JSON shape is the legacy feed-loop's (narrative +
directive_lines + topic_weights + skip_rules). The Grand Loop
witness currently only reads narrative; the other fields persist
durably in the lake delta as future signal we can reach for when
ranking, filtering, or audit becomes useful.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta, UTC

from .. import delta_client
from ..prompt import FEED_CRYSTAL_DIRECTIVE
from . import feed_orient_anchor, feed_orient_confidence, feed_orient_drift
from .llm import loop_generate

log = logging.getLogger(__name__)


# Trigger thresholds. The feed-spec says "min 10 engagement deltas
# since last regen" — keep that as the floor. Cooldown prevents
# burst-clicking from triggering a regen storm.
MIN_ENGAGEMENTS = 10
MIN_COOLDOWN_S = 30 * 60  # 30 minutes
POLL_INTERVAL_S = 60      # check every minute

# How much history we feed into the regen prompt. The directive
# wants enough signal to read intent without dragging in old noise.
# Trimmed values vs the legacy feed-loop's: a too-fat prompt was
# producing empty responses on the "hard" tier. The witness reads
# anchors, not the full engagement history; we just need enough
# signal to distill a 2-4 sentence narrative.
ENGAGEMENT_LOOKBACK_DAYS = 14
CARD_LOOKBACK_DAYS = 7
ENGAGEMENT_LIMIT = 60
CARD_LIMIT = 20
PRIOR_CRYSTAL_MAX_CHARS = 1200


_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_in_flight = False


async def _latest_feed_orient() -> dict | None:
    """Most recent crystal:feed-orient lake delta, or None if never."""
    try:
        items = await delta_client.query(
            tags_include=["crystal:feed-orient"],
            limit=1,
        )
    except Exception as e:
        log.warning("feed-orient: latest lookup failed: %s", e)
        return None
    return items[0] if items else None


async def _engagements_since(ts_iso: str | None) -> list[dict]:
    """feed-engagement deltas written since `ts_iso` (or in the
    lookback window if ts_iso is None). Newest first."""
    since = ts_iso
    if not since:
        since = (
            datetime.now(UTC) - timedelta(days=ENGAGEMENT_LOOKBACK_DAYS)
        ).isoformat()
    try:
        return await delta_client.query(
            tags_include=["feed-engagement"],
            time_start=since,
            limit=ENGAGEMENT_LIMIT,
        )
    except Exception as e:
        log.warning("feed-orient: engagement query failed: %s", e)
        return []


async def _recent_cards() -> list[dict]:
    """feed-card deltas in the last CARD_LOOKBACK_DAYS days."""
    since = (
        datetime.now(UTC) - timedelta(days=CARD_LOOKBACK_DAYS)
    ).isoformat()
    try:
        return await delta_client.query(
            tags_include=["feed-card"],
            time_start=since,
            limit=CARD_LIMIT,
        )
    except Exception as e:
        log.warning("feed-orient: card query failed: %s", e)
        return []


def _format_engagement_line(d: dict) -> str:
    tags = d.get("tags") or []
    kind = ""
    target = ""
    for t in tags:
        if isinstance(t, str) and t.startswith("engagement:"):
            kind = t.split(":", 1)[1]
        elif isinstance(t, str) and t.startswith("engages:"):
            target = t.split(":", 1)[1]
        elif isinstance(t, str) and not target and t.startswith("card:"):
            target = t.split(":", 1)[1]
    body = (d.get("content") or "").strip().split("\n", 1)[0][:160]
    ts = d.get("timestamp") or ""
    return f"  [{ts}] engagement:{kind or '?'} engages:{target[:12] or '?'} — {body}"


def _format_card_line(d: dict) -> str:
    raw = d.get("content") or ""
    title = ""
    body = ""
    try:
        payload = json.loads(raw)
        title = (payload.get("title") or "").strip()
        body = (payload.get("body") or "").strip()
    except Exception:
        body = raw.strip()
    head = title or body[:80]
    head = head.split("\n", 1)[0][:120]
    ts = d.get("timestamp") or ""
    short = (d.get("id") or "")[:12]
    route = ""
    for t in d.get("tags") or []:
        if isinstance(t, str) and t.startswith("route:"):
            route = t.split(":", 1)[1]
            break
    return f"  [{ts}] id:{short} route:{route or '?'} — {head}"


async def _build_inputs_block(prior: dict | None) -> str:
    """Format the input bundle FEED_CRYSTAL_DIRECTIVE expects."""
    prior_ts = prior.get("timestamp") if prior else None
    engagements = await _engagements_since(prior_ts)
    cards = await _recent_cards()

    parts: list[str] = []
    parts.append("RECENT ENGAGEMENT (newest first):")
    if engagements:
        parts.extend(_format_engagement_line(d) for d in engagements[:ENGAGEMENT_LIMIT])
    else:
        parts.append("  (none)")

    parts.append("\nRECENT FEED-CARDS (newest first):")
    if cards:
        parts.extend(_format_card_line(d) for d in cards[:CARD_LIMIT])
    else:
        parts.append("  (none)")

    parts.append("\nPRIOR FEED-ORIENT CRYSTAL:")
    if prior:
        prior_content = (prior.get("content") or "").strip()
        if prior_content:
            parts.append(prior_content[:PRIOR_CRYSTAL_MAX_CHARS])
        else:
            parts.append("  (empty)")
    else:
        parts.append("  (none — first regen)")

    return "\n".join(parts)


async def _run_regen() -> bool:
    """One regen pass: gather inputs, call LLM, write lake delta.
    Returns True on a successful write."""
    global _in_flight
    if _in_flight:
        return False
    _in_flight = True
    try:
        prior = await _latest_feed_orient()
        inputs = await _build_inputs_block(prior)
        prompt = f"{FEED_CRYSTAL_DIRECTIVE}\n\n{inputs}"

        log.info(
            "feed-orient regen firing (prompt %d chars; %d engagements, prior=%s)",
            len(prompt),
            len(await _engagements_since(prior.get("timestamp") if prior else None)),
            "yes" if prior else "no",
        )

        try:
            # No json_mode — the directive already says "respond with
            # ONLY a JSON object, no markdown fences", and providers
            # vary on how strict json_mode is for nested shapes (some
            # return empty when they can't produce a valid object).
            # We strip code fences + parse defensively below.
            raw = await loop_generate(
                prompt=prompt,
                tier="hard",
                max_tokens=2048,
                temperature=0.4,
                json_mode=False,
            )
        except Exception:
            log.exception("feed-orient regen LLM call failed")
            return False

        cleaned = raw.strip()
        # Strip code fences defensively (some providers wrap output).
        if cleaned.startswith("```"):
            cleaned = cleaned.lstrip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        # If the response wraps JSON in prose, extract the first { ... }
        # block. Common shape: "Here's the crystal:\n{...}".
        if cleaned and not cleaned.lstrip().startswith("{"):
            import re as _re
            m = _re.search(r"\{.*\}", cleaned, _re.DOTALL)
            if m:
                cleaned = m.group(0)

        if not cleaned:
            log.warning(
                "feed-orient regen LLM returned empty content (prompt %d chars)",
                len(prompt),
            )
            return False

        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            log.warning(
                "feed-orient regen output not JSON; first 200 chars: %r",
                cleaned[:200],
            )
            payload = {"version": 1, "narrative": cleaned[:4000]}

        narrative = (payload.get("narrative") or "").strip()
        if not narrative:
            log.warning(
                "feed-orient regen produced empty narrative; payload keys=%s",
                list(payload.keys()),
            )
            return False

        try:
            written = await delta_client.write(
                content=json.dumps(payload, ensure_ascii=False),
                tags=["crystal:feed-orient", "crystal-regen"],
                source="feed-orient",
            )
        except Exception:
            log.exception("feed-orient regen lake write failed")
            return False

        crystal_id = ""
        if isinstance(written, dict):
            crystal_id = written.get("id") or ""

        # Anchor the engagement centroid at the moment of acceptance —
        # drift now reads ~0 by construction and grows as user signals
        # diverge from what this crystal predicted. Mirrors the
        # identity-crystal anchor pattern.
        try:
            c = await delta_client.centroid(tags_include=["feed-engagement"])
            vec = c.get("centroid") if isinstance(c, dict) else None
            if vec:
                await feed_orient_anchor.save(vec, crystal_id or None)
        except Exception:
            log.exception("feed-orient regen anchor snapshot failed (non-fatal)")

        # Sample drift + confidence right away so the chart has fresh
        # post-regen points (drift resets to zero by construction;
        # confidence resets to None until new post-regen engagement
        # arrives).
        try:
            await feed_orient_drift.sample()
        except Exception:
            log.exception("feed-orient regen post-anchor drift sample failed")
        try:
            await feed_orient_confidence.sample()
        except Exception:
            log.exception("feed-orient regen post-anchor confidence sample failed")

        log.info(
            "feed-orient regen wrote crystal (narrative %d chars, %d directive lines)",
            len(narrative),
            len(payload.get("directive_lines") or []),
        )
        return True
    finally:
        _in_flight = False


async def _check_once() -> dict:
    """One pass: sample engagement-drift, count engagement signal,
    decide, maybe fire."""
    # Drift + confidence sampling runs on every tick so history
    # populates smoothly between regens. Cheap: one centroid query
    # plus one delta-query for confidence's post-regen engagements.
    try:
        await feed_orient_drift.sample()
    except Exception:
        log.exception("feed-orient drift sample failed")
    try:
        await feed_orient_confidence.sample()
    except Exception:
        log.exception("feed-orient confidence sample failed")

    prior = await _latest_feed_orient()
    prior_ts = prior.get("timestamp") if prior else None

    if prior_ts:
        try:
            elapsed = (
                datetime.now(UTC)
                - datetime.fromisoformat(prior_ts.replace("Z", "+00:00"))
            ).total_seconds()
        except Exception:
            elapsed = float("inf")
        if elapsed < MIN_COOLDOWN_S:
            return {"feed_orient": "cooldown", "elapsed": elapsed}

    engagements = await _engagements_since(prior_ts)
    n = len(engagements)
    if n < MIN_ENGAGEMENTS:
        return {"feed_orient": "below-threshold", "count": n}

    fired = await _run_regen()
    return {"feed_orient": "fired" if fired else "fire-failed", "count": n}


async def _loop() -> None:
    log.info(
        "feed-orient loop starting (min_engagements=%d, cooldown=%ds, poll=%ds)",
        MIN_ENGAGEMENTS,
        MIN_COOLDOWN_S,
        POLL_INTERVAL_S,
    )
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            await _check_once()
        except Exception:
            log.exception("feed-orient poll error")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                _stop_event.wait(),
                timeout=POLL_INTERVAL_S,
            )
    log.info("feed-orient loop stopped")


def start() -> None:
    """Kick off the polling task. Idempotent."""
    global _task, _stop_event
    if _task is not None and not _task.done():
        return
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_loop(), name="loop/feed-orient")


async def stop() -> None:
    """Signal the loop to exit. Awaits the task briefly."""
    global _task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except TimeoutError:
            _task.cancel()
        except Exception:
            pass
    _task = None
    _stop_event = None
