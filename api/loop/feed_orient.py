"""Feed-orient crystal regeneration.

Distills the user's accumulated engagement (the +/- markers, chats
from cards) into a short narrative that the routing layer uses to
decide what the feed should surface.

Trigger paths (all bypass cooldown except the manual button):
  · Thumb trigger  — on_engagement_written() called after each +/-;
                     fires when THUMB_REGEN_THRESHOLD thumbs have
                     accumulated since the last crystal.
  · Harness signal — regen_from_signal() called by the orient_shift
                     harness tool when a fire reveals orientation shift.
  · Manual button  — force_run_regen() in the dashboard; 30-min
                     cooldown so spamming can't pin a regen storm.

Output JSON: {narrative, directive_lines, topic_weights, skip_rules}.
The routing layer reads `narrative`; other fields persist in the lake
delta as future signal.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, UTC

from .. import delta_client
from ..prompt import FEED_CRYSTAL_DIRECTIVE
from .llm import loop_generate

log = logging.getLogger(__name__)


MIN_COOLDOWN_S = 30 * 60  # manual button cooldown only

# Number of thumb signals since the last crystal that triggers an
# automatic regen. Bypasses the cooldown — explicit engagement is
# direct signal.
THUMB_REGEN_THRESHOLD = 5

ENGAGEMENT_LOOKBACK_DAYS = 14
CARD_LOOKBACK_DAYS = 7
ENGAGEMENT_LIMIT = 60
CARD_LIMIT = 20
PRIOR_CRYSTAL_MAX_CHARS = 1200

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
    """feed-engagement deltas written since `ts_iso` (or within the
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
            "feed-orient regen firing (prompt %d chars; prior=%s)",
            len(prompt),
            "yes" if prior else "no",
        )

        try:
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
        if cleaned.startswith("```"):
            cleaned = cleaned.lstrip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        if not cleaned:
            log.warning(
                "feed-orient regen LLM returned empty content (prompt %d chars)",
                len(prompt),
            )
            return False

        import re as _re
        m = _re.search(r"\{.*\}", cleaned, _re.DOTALL)
        if m:
            cleaned = m.group(0)

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
            await delta_client.write(
                content=json.dumps(payload, ensure_ascii=False),
                tags=["crystal:feed-orient", "crystal-regen"],
                source="feed-orient",
            )
        except Exception:
            log.exception("feed-orient regen lake write failed")
            return False

        try:
            from .router import invalidate_crystal_cache
            invalidate_crystal_cache()
        except Exception:
            pass

        log.info(
            "feed-orient regen wrote crystal (narrative %d chars, %d directive lines)",
            len(narrative),
            len(payload.get("directive_lines") or []),
        )
        return True
    finally:
        _in_flight = False


async def regen_from_signal() -> None:
    """Signal-triggered regen — no cooldown check. Called by the thumb
    trigger (THUMB_REGEN_THRESHOLD engagements) and the harness
    orient_shift tool. In-flight guard still applies."""
    if _in_flight:
        log.debug("feed-orient regen_from_signal: already in flight, skipping")
        return
    log.info("feed-orient regen firing (reason=signal)")
    await _run_regen()


async def on_engagement_written() -> None:
    """Call after each feed-engagement delta is written. Fires regen
    when THUMB_REGEN_THRESHOLD thumbs have accumulated since the last
    crystal. Safe to fire-and-forget via asyncio.create_task."""
    try:
        prior = await _latest_feed_orient()
        prior_ts = prior.get("timestamp") if prior else None
        engagements = await _engagements_since(prior_ts)
        if len(engagements) >= THUMB_REGEN_THRESHOLD:
            await regen_from_signal()
    except Exception as e:
        log.warning("feed-orient on_engagement_written failed: %s", e)


async def force_run_regen() -> dict:
    """Manual fire path (dashboard button). Cooldown applies so
    spamming can't pin a regen storm.

    Returns:
      · {"fired": True, ...}
      · {"fired": False, "reason": "cooldown", "elapsed": <sec>}
      · {"fired": False, "reason": "in-flight"}
      · {"fired": False, "reason": "fire-failed"}
    """
    if _in_flight:
        return {"fired": False, "reason": "in-flight"}

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
            return {
                "fired": False,
                "reason": "cooldown",
                "elapsed": int(elapsed),
                "cooldown_seconds": MIN_COOLDOWN_S,
            }

    log.info("feed-orient regen firing (reason=manual)")
    fired = await _run_regen()
    return {
        "fired": bool(fired),
        "reason": "manual" if fired else "fire-failed",
    }


def start() -> None:
    """No-op — regen is now event-driven (thumbs + harness tool).
    Kept for interface compatibility with worker.py."""


async def stop() -> None:
    """No-op — no background task to stop."""
