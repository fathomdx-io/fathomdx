"""Feed loop — page-view-debounced consumer of the feed-orient crystal.

The crystal lives in api/feed_crystal.py and answers "what to put in
the user's feed right now." This module answers "when, and what cards
land." It runs in-process inside consumer-api — no agent, no routine,
no external scheduler. The dashboard load is the wake event.

Each fire:
  1. Wake-gate the crystal (regen if drift/confidence say so).
  2. Read the latest crystal.
  3. For each directive_line:
     • Skip if a fresh-enough card already exists.
     • Otherwise spend a budget on fathom_think to produce one card.
     • Write a `feed-card` delta tagged back to the directive line.
  4. Return.

A single-flight lock keeps simultaneous visits from firing the loop
twice. Status is exposed via `current_status()` so the UI indicator can
show "generating…" while it runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from . import delta_client, feed_crystal, feed_pressure
from . import messages as messages_mod
from ._bgtasks import spawn as _spawn_task
from ._feed_candidates import (
    _fetch_line_candidates,
    _format_candidates,
)
from ._feed_card_body import (
    MAX_FORMAT_ATTEMPTS,
    _candidate_hashes,
    _candidate_image_urls,
    _parse_card_payload,
    _validate_body_image,
    _validate_media_list,
)
from ._time import now as _now
from .settings import settings

log = logging.getLogger(__name__)
# uvicorn's default config keeps app loggers at WARNING. Pin to INFO so the
# feed-loop's per-line decisions land in `podman logs` for debugging.
logging.getLogger(__name__).setLevel(logging.INFO)

CARD_TAG = "feed-card"
CARD_SOURCE = "fathom-feed"

# How many recent feed cards to inline into the per-card directive so
# the model can dedup against what was already published and write
# something that segues into the running narrative. Five is a balance
# between useful context and prompt bloat — body excerpts cap at ~200
# chars each, so 5 cards ≈ 1.5KB of context.
_RECENT_CARDS_FOR_PROMPT = 5

# Below this many real candidates for a slot, don't fire. The model's
# text isn't grounded against candidates (only body_image + media
# hashes are validated), so an empty or near-empty pool hands it
# nothing to lean on and it hallucinates from its training prior.
# Seen in the wild: a fresh install with no sources produced six
# confabulated JWST news cards in a row because every slot kept firing
# with zero candidates. One real candidate is the absolute floor;
# raise this if you'd rather demand more grounding before a card writes.
MIN_CANDIDATES_TO_FIRE = 1


def _contact_tag(contact_slug: str) -> str:
    return f"contact:{contact_slug}"


def _empty_status() -> dict[str, Any]:
    return {
        "generating": False,
        "started_at": None,
        "finished_at": None,
        "lines_total": 0,
        "lines_done": 0,
        "last_reason": None,
        # True only while an LLM call is actually in flight (crystal
        # synthesis or card production via fathom_think). Distinct from
        # `generating` — a no-op run where all directive lines are fresh
        # never fires an LLM, so `llm_active` stays false throughout and
        # the UI pulse correctly doesn't flash.
        "llm_active": False,
        "llm_active_count": 0,  # counter so concurrent calls nest cleanly
        # Short human-readable label of what the current LLM call is
        # actually doing — "Updating feed directive", "Generating card:
        # wolves-of-yellowstone", etc. Set before entering an LLM-bounded
        # section, cleared when llm_active_count returns to zero.
        "activity_label": None,
        # Populated when a run finishes. Tells the UI what the most recent
        # visit actually produced — often zero cards (all topics fresh, no
        # directive lines yet), which without this field is indistinguishable
        # from "the system is broken" to anyone watching the page.
        "last_outcome": None,  # {summary, detail, cards_written, at}
    }


def _llm_active_enter(contact_slug: str, label: str | None = None) -> None:
    st = _status.setdefault(contact_slug, _empty_status())
    st["llm_active_count"] = st.get("llm_active_count", 0) + 1
    st["llm_active"] = True
    if label:
        st["activity_label"] = label


def _llm_active_exit(contact_slug: str) -> None:
    st = _status.setdefault(contact_slug, _empty_status())
    n = max(0, st.get("llm_active_count", 0) - 1)
    st["llm_active_count"] = n
    st["llm_active"] = n > 0
    if n == 0:
        st["activity_label"] = None


# Per-run tallies, reset in _run_once and folded into last_outcome at the
# end. Split out from the public status dict so we can distinguish "zero
# because nothing fired yet" from "zero because every line was fresh" at
# summarize-time.
_run_tallies: dict[str, dict[str, int]] = {}


def _tally_reset(contact_slug: str) -> None:
    _run_tallies[contact_slug] = {
        "cards_written": 0,
        "lines_skipped_fresh": 0,
        "lines_timed_out": 0,
        "lines_model_skipped": 0,
        "lines_format_failed": 0,
        "lines_missing_fields": 0,
        # Off-crystal passes — drift + volunteered. Counted separately from
        # `cards_written` (which is the per-line tally) so the summary can
        # narrate them distinctly: "2 slotted + 1 drift + 0 notice" reads
        # differently from "3 cards."
        "drift_cards_written": 0,
        "drift_silent": 0,  # model returned {"cards": []} by choice
        "drift_timed_out": 0,
        "drift_format_failed": 0,
        "volunteered_cards_written": 0,
        "volunteered_silent": 0,
        "volunteered_timed_out": 0,
        "volunteered_format_failed": 0,
        # New synthesis-rebuild passes. Same tally shape as drift /
        # volunteered — written / silent / timed-out / format-failed.
        "alert_cards_written": 0,
        "alert_silent": 0,
        "alert_timed_out": 0,
        "alert_format_failed": 0,
        "reflection_cards_written": 0,
        "reflection_silent": 0,
        "reflection_timed_out": 0,
        "reflection_format_failed": 0,
        "bridging_cards_written": 0,
        "bridging_silent": 0,
        "bridging_timed_out": 0,
        "bridging_format_failed": 0,
        "discrepancy_cards_written": 0,
        "discrepancy_silent": 0,
        "discrepancy_timed_out": 0,
        "discrepancy_format_failed": 0,
        # Router-level drop counter. Distinct from the per-pass silent
        # counters: silent = the pass itself returned no cards; dropped
        # = the pass returned a card but the judge+router decided it
        # wasn't worth writing.
        "dropped_by_router": 0,
    }


def _tally_inc(contact_slug: str, key: str) -> None:
    t = _run_tallies.get(contact_slug)
    if t is not None:
        t[key] = t.get(key, 0) + 1


def _summarize_outcome(contact_slug: str, had_crystal: bool, had_lines: bool) -> dict:
    """Fold per-run tally + structural facts (crystal, lines) into a one-line
    outcome the UI can render as a status pip + tooltip. Summary values are
    stable identifiers the frontend switches on; detail is the human string.
    """
    t = _run_tallies.get(contact_slug) or {}
    cards = t.get("cards_written", 0)
    fresh = t.get("lines_skipped_fresh", 0)
    timeouts = t.get("lines_timed_out", 0)
    skipped = t.get("lines_model_skipped", 0)
    format_fail = t.get("lines_format_failed", 0)
    missing = t.get("lines_missing_fields", 0)
    drift_cards = t.get("drift_cards_written", 0)
    volunteered_cards = t.get("volunteered_cards_written", 0)
    off_crystal_cards = drift_cards + volunteered_cards
    total_cards = cards + off_crystal_cards
    at = _now().isoformat()

    if not had_crystal:
        return {
            "summary": "cold_start",
            "detail": (
                "No crystal yet — ran one broad curiosity card. "
                "Engage with a few cards (thumbs, clicks) and a real feed directive forms."
            ),
            "cards_written": cards,
            "at": at,
        }
    if not had_lines:
        return {
            "summary": "no_directives",
            "detail": (
                "The crystal has no directive lines. Keep engaging — the next "
                "crystal regen will derive them from your signals."
            ),
            "cards_written": cards,
            "at": at,
        }
    if total_cards > 0:
        breakdown_parts: list[str] = []
        if cards:
            breakdown_parts.append(f"{cards} slotted")
        if drift_cards:
            breakdown_parts.append(f"{drift_cards} drift")
        if volunteered_cards:
            breakdown_parts.append(f"{volunteered_cards} noticed")
        plural = "s" if total_cards != 1 else ""
        if len(breakdown_parts) > 1:
            detail = f"Wrote {total_cards} new card{plural} ({', '.join(breakdown_parts)})."
        else:
            detail = f"Wrote {total_cards} new card{plural}."
        return {
            "summary": "generated",
            "detail": detail,
            "cards_written": total_cards,
            "at": at,
        }
    # No cards written despite having a crystal + lines. Distinguish:
    #   - "failures" = timeouts, format-failed, missing-title/body. These
    #     mean the LLM broke the contract and we lost a slot to noise.
    #   - "model_skipped" = the LLM explicitly returned {"skip": true}.
    #     That's a design-intended outcome — the prompt tells it to skip
    #     when no candidate fits — and should be treated as calmly as
    #     "fresh": nothing was written because nothing was warranted.
    failures = timeouts + format_fail + missing
    all_acquitted = fresh + skipped  # nothing actually went wrong
    dropped = t.get("dropped_by_router", 0)
    if failures == 0 and dropped > 0 and all_acquitted == 0:
        # Great no-news day: every pass ran, candidates were produced,
        # but the judge+router decided nothing was worth surfacing. This
        # is a healthy state by design — the synthesis layer is allowed
        # to stay quiet. UI renders this distinctly from "all_fresh"
        # (which means cards exist but are still warm) so the user can
        # tell the difference between "nothing happened" and "the system
        # actively considered everything and chose silence."
        plural = "s" if dropped != 1 else ""
        return {
            "summary": "great_no_news",
            "detail": (
                f"It's a great no-news day. The lake is steady — "
                f"{dropped} candidate{plural} considered, none crossed the "
                f"surface threshold."
            ),
            "cards_written": 0,
            "at": at,
        }
    if failures == 0 and all_acquitted > 0:
        # Calm zero-card outcome. Narrate the mix so the tooltip still
        # informs, but classify as all_fresh so the UI stays gray.
        parts = []
        if fresh:
            parts.append(f"{fresh} already-fresh")
        if skipped:
            plural = "s" if skipped != 1 else ""
            parts.append(f"{skipped} model-pass{'es' if skipped != 1 else ''}")
        if dropped:
            plural = "s" if dropped != 1 else ""
            parts.append(f"{dropped} dropped-by-judge")
        return {
            "summary": "all_fresh",
            "detail": (f"Nothing needed generating ({', '.join(parts)}) — the feed is caught up."),
            "cards_written": 0,
            "at": at,
        }
    # At least one real failure — warn state.
    reasons = []
    if timeouts:
        reasons.append(f"{timeouts} timed out")
    if format_fail:
        reasons.append(f"{format_fail} format-failed")
    if missing:
        reasons.append(f"{missing} missing title/body")
    if fresh:
        reasons.append(f"{fresh} already-fresh")
    if skipped:
        reasons.append(f"{skipped} model-pass")
    return {
        "summary": "no_cards",
        "detail": (f"Ran, but no cards were written ({', '.join(reasons) or 'unknown reason'})."),
        "cards_written": 0,
        "at": at,
    }


# Per-contact single-flight locks. One contact's feed fire shouldn't block
# another's — each contact gets its own asyncio.Lock, minted lazily on first use.
_run_locks: dict[str, asyncio.Lock] = {}

# Per-contact UI status. Read by /v1/feed/status for the "generating…"
# indicator, written atomically inside the matching lock.
_status: dict[str, dict[str, Any]] = {}

# Per-contact pending-fire tasks. mark_visit() spawns one of these when
# pressure says fire; we hold the handle so a flurry of visits inside the
# same fire don't pile up duplicate tasks. Cadence itself comes from
# feed_pressure.should_synthesize() — there is no wall-clock debounce.
_pending_visits: dict[str, asyncio.Task] = {}


def _lock_for(contact_slug: str) -> asyncio.Lock:
    lock = _run_locks.get(contact_slug)
    if lock is None:
        lock = asyncio.Lock()
        _run_locks[contact_slug] = lock
    return lock


def current_status(contact_slug: str) -> dict:
    """Snapshot for the /v1/feed/status endpoint, scoped to one contact."""
    return dict(_status.get(contact_slug) or _empty_status())


def _set_status(contact_slug: str, **kwargs) -> None:
    st = _status.setdefault(contact_slug, _empty_status())
    st.update(kwargs)


# ── Visit debouncer ──────────────────────────────────────────────────────


async def mark_visit(contact_slug: str) -> dict:
    """No-op since the Grand Loop cutover. The legacy feed pipeline is
    retired — no per-line / drift / volunteer / reflection / discrepancy
    / alert passes fire anymore. The endpoint stays as a 200 so any
    leftover pinger doesn't 404; the loop never schedules.
    """
    return {"scheduled": False, "reason": "legacy-feed-retired"}


async def force_fire(contact_slug: str, reason: str = "manual") -> dict:
    """Fire the loop immediately, skipping the visit-debounce cooldown.

    Used by `POST /v1/feed/refresh` (the existing manual-kick endpoint).
    Still respects this contact's single-flight lock.
    """
    if _lock_for(contact_slug).locked():
        return {"fired": False, "reason": "already-running"}
    _spawn_task(_run_once(contact_slug, reason=reason), name=f"feed-loop/{contact_slug}")
    return {"fired": True}


# ── The loop itself ──────────────────────────────────────────────────────


async def _run_once(contact_slug: str, reason: str = "unspecified") -> None:
    lock = _lock_for(contact_slug)
    if lock.locked():
        return
    async with lock:
        started = _now().isoformat()
        _tally_reset(contact_slug)
        _set_status(
            contact_slug,
            generating=True,
            started_at=started,
            finished_at=None,
            lines_total=0,
            lines_done=0,
            last_reason=reason,
        )
        # Structural facts captured by _do_run via closure so the summary
        # knows whether the loop actually had a crystal or directive lines
        # to work with.
        run_facts = {"had_crystal": False, "had_lines": False}
        try:
            await _do_run(contact_slug, reason, run_facts)
        except Exception:
            log.exception("feed_loop: run failed (contact=%s)", contact_slug)
        finally:
            outcome = _summarize_outcome(
                contact_slug, run_facts["had_crystal"], run_facts["had_lines"]
            )
            _set_status(
                contact_slug, generating=False, finished_at=_now().isoformat(), last_outcome=outcome
            )
            # Synthesis sat with what had built up — reset the pressure
            # meter so the next "newly calmed" can fire. We mark even on
            # exception: a partial run still consumed the lake material it
            # was going to consume.
            try:
                await feed_pressure.mark_synthesis()
            except Exception:
                log.exception("feed_loop: feed_pressure.mark_synthesis failed")
            # Stamp the regen as a queryable delta so the Weather Stats
            # graph (and anything else timeline-shaped) can show "the
            # feed sat with itself here." Best-effort — chart markers
            # aren't worth failing a run over.
            try:
                await delta_client.write(
                    content=json.dumps({
                        "reason": reason,
                        "started_at": started,
                        "finished_at": _now().isoformat(),
                        "outcome": outcome,
                    }),
                    tags=["feed-regen-event", _contact_tag(contact_slug)],
                    source="fathom-feed",
                )
            except Exception:
                log.exception("feed_loop: failed to write feed-regen-event delta")


async def _do_run(contact_slug: str, reason: str, run_facts: dict) -> None:
    # Wake-gate the crystal. The predicate checks drift, confidence, and
    # the cold-start min-signal guard — see api/feed_crystal.should_regen.
    try:
        should, why = await feed_crystal.should_regen(contact_slug)
    except Exception:
        print(
            f"feed_loop[{contact_slug}]: should_regen check failed; proceeding without regen",
            flush=True,
        )
        should, why = False, "predicate-error"
    print(
        f"feed_loop[{contact_slug}]: wake reason={reason}, regen-decision={should} ({why})",
        flush=True,
    )
    if should:
        _llm_active_enter(contact_slug, label="Rederiving feed directive from engagement")
        try:
            await feed_crystal.synthesize(contact_slug)
        except Exception as e:
            print(
                f"feed_loop[{contact_slug}]: crystal synthesize failed: {type(e).__name__}: {e}; using stale crystal",
                flush=True,
            )
        finally:
            _llm_active_exit(contact_slug)

    crystal = await feed_crystal.latest(contact_slug, force=True)

    # New synthesis-rebuild passes. These run regardless of crystal state
    # — alerts especially must fire on a bare-install lake. Order is
    # priority: alert (piercing) → reflection → bridging → discrepancy.
    # Each pass self-silences when nothing meaningful is there; the
    # judge+router gates each candidate before it lands.
    for pass_name, pass_fn in (
        ("alert", _fire_alert),
        ("reflection", _fire_reflection),
        ("bridging", _fire_bridging),
        ("discrepancy", _fire_discrepancy),
    ):
        try:
            await pass_fn(contact_slug, crystal)
        except Exception as e:
            print(
                f"feed_loop[{contact_slug}]: {pass_name} pass failed: {type(e).__name__}: {e}",
                flush=True,
            )

    if not crystal:
        # Cold-start path — no crystal yet, no signal yet either. Run a
        # broadly-curious single fire so the lake gets some sediment we
        # can later distill from.
        print(f"feed_loop[{contact_slug}]: cold-start path (no crystal)", flush=True)
        await _cold_start_fire(contact_slug)
        return

    run_facts["had_crystal"] = True
    lines = crystal.get("directive_lines") or []
    if not lines:
        print(f"feed_loop[{contact_slug}]: crystal has no directive lines; skipping", flush=True)
        return
    run_facts["had_lines"] = True

    print(
        f"feed_loop[{contact_slug}]: crystal id={crystal.get('id')}, {len(lines)} directive line(s)",
        flush=True,
    )
    # Batch-prefetch "newest card per directive line" in ONE lake query
    # instead of N (was a classic N+1: one _has_fresh_card query per
    # line per visit). For 10 directive lines, that's 10 round-trips
    # saved. The map is {directive_line_id: latest_iso_timestamp}.
    freshness_map = await _latest_card_by_line(contact_slug)

    _set_status(contact_slug, lines_total=len(lines), lines_done=0)
    for i, line in enumerate(lines):
        try:
            await _fire_line(contact_slug, line, crystal, freshness_map)
        except Exception as e:
            print(
                f"feed_loop[{contact_slug}]: line {line.get('id')} failed: {type(e).__name__}: {e}",
                flush=True,
            )
        _set_status(contact_slug, lines_done=i + 1)
    print(f"feed_loop[{contact_slug}]: {len(lines)} directive line(s) processed", flush=True)

    # Off-crystal passes. Both run unconditionally every fire and self-silence
    # when nothing resonates / nothing stood out. Order matters a little —
    # drift runs first so volunteered's candidate dedupe doesn't have to think
    # about drift cards (drift writes to the lake with source=fathom-feed,
    # which volunteered's _EXCLUDE_SOURCES already filters out).
    try:
        await _fire_drift(contact_slug, crystal)
    except Exception as e:
        print(
            f"feed_loop[{contact_slug}]: drift pass failed: {type(e).__name__}: {e}",
            flush=True,
        )
    try:
        await _fire_volunteered(contact_slug, crystal)
    except Exception as e:
        print(
            f"feed_loop[{contact_slug}]: volunteered pass failed: {type(e).__name__}: {e}",
            flush=True,
        )
    print(f"feed_loop[{contact_slug}]: run complete", flush=True)


def _humanize_age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


async def _recent_feed_cards_block(contact_slug: str, limit: int = _RECENT_CARDS_FOR_PROMPT) -> str:
    """Prompt block listing recent feed cards already shown to this contact.

    Sibling to messages.dm_context_block. Both prepend to card-production
    directives so the model writes against a visible history rather than
    re-deriving from candidates alone. Without this, drift and volunteered
    passes can repeat themselves across runs because the candidate pool
    re-surfaces similar deltas.

    Each entry includes title + body excerpt + relative timestamp so the
    model dedupes against what was *said*, not just what was titled.
    """
    if not contact_slug:
        return ""
    try:
        results = await delta_client.query(
            tags_include=[CARD_TAG, _contact_tag(contact_slug)],
            limit=limit,
        )
    except Exception:
        return ""
    if not results:
        return "=== RECENT FEED CARDS ===\n(no feed cards published yet)"

    now = datetime.now()
    if now.tzinfo is None:
        from datetime import UTC as _UTC

        now = datetime.now(_UTC)

    lines = [
        "=== RECENT FEED CARDS ===",
        (
            "Cards you've already published to the feed (newest first). "
            "Avoid repeating points or images the reader has just seen; "
            "instead, extend, contrast, or move to fresh ground. If a new "
            "output would meaningfully cover the same territory, prefer "
            "to skip or to set `direct: true` and route it as a DM "
            "rather than re-publishing."
        ),
        "",
    ]
    for d in results:
        try:
            payload = json.loads(d.get("content") or "{}")
        except Exception:
            payload = {}
        title = (payload.get("title") or payload.get("kicker") or "").strip()
        body = (payload.get("body") or "").strip().replace("\n", " ")
        if len(body) > 200:
            body = body[:200] + "…"
        ts = d.get("timestamp") or ""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = _humanize_age((now - dt).total_seconds())
        except Exception:
            age = ts or "unknown"
        if title and body:
            lines.append(f"  [{age}] {title}")
            lines.append(f"           {body}")
        elif title or body:
            lines.append(f"  [{age}] {title or body}")
    return "\n".join(lines)


_CARD_OUTPUT_INSTRUCTIONS = (
    "Respond with ONLY a JSON object — no markdown fences, no commentary.\n"
    "Schema:\n"
    "  {\n"
    '    "title": string                       // one-sentence headline (≤120 chars). Required for feed cards; ignored when direct=true.\n'
    '    "body":  string                       // For feed cards: 2-4 sentences of plain prose. For direct messages: the message text.\n'
    '    "tail":  string?                      // ≤8 words. Source citation, timestamp, stat, or next step. SKIP if you have nothing concrete — empty is better than restating the title.\n'
    '    "body_image": string?                 // media_hash or URL\n'
    '    "body_image_layout": "hero" | "thumb" // default "hero"\n'
    '    "media": string[]?                    // additional images\n'
    '    "link": string?                       // primary source URL — must start with http(s)\n'
    '    "links": [{title: string, url: string}]?  // additional related links (bundling)\n'
    '    "direct": boolean?                    // OPTIONAL. true = route this output as a direct message to the contact instead of publishing it as a feed card. Use rarely — see the DIRECT MESSAGES block at the top of this prompt for the cadence rules. When direct=true, only `body` is used; the other fields are ignored.\n'
    "  }\n"
    "\n"
    "IMAGES — if the deltas you searched contain images (a media_hash on a "
    "delta, or markdown image URLs like ![](https://…)), include the strongest "
    "one in body_image and any extras in media. A weather card without weather "
    "imagery, a science card without a diagram, an RSS post without its photo — "
    "these are broken cards. The reader came for the picture as much as the prose.\n"
    "\n"
    "LINKS — if a candidate is marked 🔗[link=…], include that URL in `link`. The "
    "RSS source plugin appends `[Source](url)` to every item, so the link is the "
    "canonical article. If you bundled multiple candidates, the strongest goes in "
    "`link` and the rest go in `links` with short descriptive titles. A card without "
    "a link is a card without provenance — always include one when the candidate has "
    "it. Copy the URL exactly; do not paraphrase or invent.\n"
    "\n"
    "BUNDLING — if your search returns several deltas on the same topic or moment, "
    "you can compose ONE card that synthesizes across them. Pick the single strongest "
    "image for body_image, gather other notable images into media, the canonical link "
    "in `link`, the rest in `links`, and let the body reference what they have in "
    "common. Better one rich card than three thin ones.\n"
    "\n"
    "If you genuinely cannot satisfy the slot (no real answer exists, or a "
    'SKIP rule fires), respond with `{"skip": true, "reason": "<short>"}` instead.\n'
)


async def _cold_start_fire(contact_slug: str) -> None:
    """One broad-strokes card when there's no crystal and no engagement yet."""
    directive = (
        f"There's no feed-orient crystal yet — this reader ({contact_slug}) has "
        "not given any signal about what they want in their feed. Pick ONE "
        "genuinely interesting thing happening in the world right now "
        "(curiosity-default), search the web or the lake for an authoritative "
        "source, and produce a single feed card.\n\n" + _CARD_OUTPUT_INSTRUCTIONS
    )
    _llm_active_enter(contact_slug, label="First card — picking something curious")
    try:
        await asyncio.wait_for(
            _produce_card(contact_slug, line=None, crystal=None, directive=directive),
            timeout=settings.feed_loop_budget_seconds,
        )
    except TimeoutError:
        log.info("feed_loop: cold-start fire timed out (contact=%s)", contact_slug)
    finally:
        _llm_active_exit(contact_slug)


async def _fire_line(
    contact_slug: str,
    line: dict,
    crystal: dict,
    freshness_map: dict[str, str] | None = None,
) -> None:
    """One directive line → one feed card (subject to freshness check).

    `freshness_map` (optional) is the batch-prefetched {line_id: latest_ts}
    from `_latest_card_by_line`. When supplied, the freshness check skips
    a per-line lake query. Callers that don't pass it (e.g. cold-start
    single-fire) fall through to the per-line lookup, which is still
    correct — just slower.
    """
    line_id = (line.get("id") or "").strip() or "unnamed"
    topic = (line.get("topic") or "").strip()
    freshness_h = float(line.get("freshness_hours") or 12)

    # Freshness check — skip if this contact already has a card for this
    # line that's newer than the freshness window.
    if freshness_map is not None:
        is_fresh = _is_fresh_from_map(freshness_map, line_id, freshness_h)
    else:
        is_fresh = await _has_fresh_card(contact_slug, line_id, freshness_h)
    if is_fresh:
        print(
            f"feed_loop[{contact_slug}]: line {line_id} skipped (fresh card exists, window={freshness_h}h)",
            flush=True,
        )
        _tally_inc(contact_slug, "lines_skipped_fresh")
        return
    print(
        f"feed_loop[{contact_slug}]: line {line_id} firing (topic={line.get('topic')}, weight={line.get('weight')})",
        flush=True,
    )

    # Pre-fetch candidates so the model isn't betting on semantic-search
    # to surface the right content. See _fetch_line_candidates.
    candidates = await _fetch_line_candidates(line, limit=20)
    print(f"feed_loop: line {line_id} candidates pre-fetched: {len(candidates)}", flush=True)

    # Grounding guard: if the lake has nothing (or barely anything) to
    # anchor this slot, skip rather than fire the model. Text fields
    # don't go through the hash/URL validator, so an empty candidate
    # pool means whatever prose the model returns is pure prior — i.e.
    # hallucinated. Better to emit no card than a plausible-looking lie.
    if len(candidates) < MIN_CANDIDATES_TO_FIRE:
        print(
            f"feed_loop[{contact_slug}]: line {line_id} skipped — "
            f"{len(candidates)} candidates (<{MIN_CANDIDATES_TO_FIRE}); "
            f"nothing grounded to write from",
            flush=True,
        )
        _tally_inc(contact_slug, "lines_skipped_no_candidates")
        return

    candidates_block = _format_candidates(candidates)

    skip_if = (line.get("skip_if") or "").strip()
    skip_clause = f"\nSKIP CONDITION: {skip_if}" if skip_if else ""
    skip_rules = crystal.get("skip_rules") or []
    skip_block = ("\nGENERAL SKIP RULES:\n  - " + "\n  - ".join(skip_rules)) if skip_rules else ""

    directive = (
        f"You are filling one slot in the user's feed.\n\n"
        f"OVERALL FEED ORIENTATION (from the crystal):\n{crystal.get('narrative') or '(none)'}\n\n"
        f"THIS SLOT:\n"
        f"  id:      {line_id}\n"
        f"  topic:   {topic or '(none)'}\n"
        f"  weight:  {line.get('weight') or 'unspecified'}\n"
        f"  freshness window: {freshness_h}h"
        f"{skip_clause}{skip_block}\n\n"
        f"=== CANDIDATES FROM THE LAKE (pre-fetched, sorted newest first) ===\n"
        f"{candidates_block}\n\n"
        f"Pick the strongest candidate (or two related ones — see BUNDLING) and "
        f"write the card. Image preference, in order:\n"
        f"  1. PREFER 📷[hash=…] — copy the hash EXACTLY into body_image (16 hex "
        f"chars, no truncation, no paraphrasing). Hashes are in-lake, stable, and "
        f"always render.\n"
        f"  2. Fall back to 🖼[url=…] only when no hash is available — copy the URL "
        f"exactly. External URLs can be signed/expiring (imgproxy, CDNs) and may "
        f"404 by render time, so they're second choice.\n"
        f"For links: 🔗[link=…] — copy that URL into the `link` field exactly. If you "
        f"bundled multiple candidates, the strongest goes in `link` and the rest in "
        f"`links`. Cards without a link feel orphaned; always include one when any "
        f"candidate has it.\n"
        f"If you invent a hash, the validator drops it. If you invent a URL — including "
        f"placeholder services like picsum.photos — the validator drops that too: a "
        f"body_image URL must appear verbatim in one of the candidates above. Don't "
        f"paraphrase, don't swap a seed, don't reach for a generic stock image. If the "
        f"candidates don't fit, you can still call the search tools — but candidates are "
        f"the cheap path and usually contain what you need.\n\n" + _CARD_OUTPUT_INSTRUCTIONS
    )

    label_topic = topic or line_id
    _llm_active_enter(contact_slug, label=f"Generating card: {label_topic}")
    try:
        await asyncio.wait_for(
            _produce_card(
                contact_slug,
                line=line,
                crystal=crystal,
                directive=directive,
                candidates=candidates,
            ),
            timeout=settings.feed_loop_budget_seconds,
        )
    except TimeoutError:
        print(f"feed_loop[{contact_slug}]: line {line_id} timed out", flush=True)
        _tally_inc(contact_slug, "lines_timed_out")
    finally:
        _llm_active_exit(contact_slug)


async def _produce_card(
    contact_slug: str,
    line: dict | None,
    crystal: dict | None,
    directive: str,
    candidates: list[dict] | None = None,
    kind: str = "per_line",
) -> None:
    """Run fathom_think; parse the JSON-shaped final assistant message; write a card.

    Retries on non-JSON output up to MAX_FORMAT_ATTEMPTS, each time feeding
    the previous garbled output back to the model with a louder format
    nudge. The whole call is still bounded by the slot's wall-clock budget
    (`asyncio.wait_for` in the caller) — retries don't get bonus time.

    `candidates` is the pre-fetched pool used to validate body_image and
    media values — drops any hash the model invented that isn't in the lake.
    """
    from .server import fathom_think  # lazy — avoid circular import

    line_id = (line or {}).get("id") or "(cold-start)"

    # Prepend the DM-routing and recent-feed-card context blocks. The
    # model sees recent DMs (for cadence + dedup of direct messages),
    # recent published feed cards (for narrative continuity + dedup of
    # what was already shown), and learns it can route output as a DM
    # by setting `direct: true`. Empty blocks are silent noops.
    dm_block = await messages_mod.dm_context_block(contact_slug)
    feed_block = await _recent_feed_cards_block(contact_slug)
    prelude = "\n\n".join(b for b in (dm_block, feed_block) if b)
    if prelude:
        directive = prelude + "\n\n" + directive

    user_message = "Produce the card for the slot described above."
    last_failed_excerpt: str | None = None
    payload: dict | None = None

    for attempt in range(1, MAX_FORMAT_ATTEMPTS + 1):
        # On retries, prepend a stronger format-correction nudge that
        # quotes the previous failed output so the model sees what it did.
        if attempt > 1 and last_failed_excerpt is not None:
            nudge = (
                f"⚠ Your previous attempt was not valid JSON. The output started with:\n"
                f"---\n{last_failed_excerpt}\n---\n\n"
                f"Attempt {attempt} of {MAX_FORMAT_ATTEMPTS}. Respond with ONLY the "
                f"JSON object specified in the directive above. No prose, no markdown "
                f"fences, no commentary. Just the object."
            )
            this_message = nudge + "\n\n" + user_message
            # Skip the search/tool work on retries — the failed prior attempt
            # already had a chance. Tighten the round budget so a bad retry
            # can't burn more wall-clock than necessary.
            this_max_rounds = max(2, settings.feed_loop_budget_tool_calls // 3)
        else:
            this_message = user_message
            this_max_rounds = settings.feed_loop_budget_tool_calls

        messages = await fathom_think(
            user_message=this_message,
            directive=directive,
            recall=False,
            max_rounds=this_max_rounds,
        )
        last = messages[-1] if messages else {}
        text = (last.get("content") or "").strip()
        if not text:
            log.info("feed_loop: line %s attempt %d — empty final message", line_id, attempt)
            last_failed_excerpt = "(empty message)"
            continue

        candidate = _parse_card_payload(text)
        if candidate is None:
            print(
                f"feed_loop: line {line_id} attempt {attempt} — non-JSON; will retry. excerpt: {text[:200]!r}",
                flush=True,
            )
            last_failed_excerpt = text[:240].replace("\n", " ")
            continue

        # Got valid JSON. Stop here even if the payload turns out to be
        # malformed-but-valid (e.g. missing fields) — that's a content
        # problem, not a format problem, and retrying won't help.
        payload = candidate
        if attempt > 1:
            print(f"feed_loop: line {line_id} recovered on attempt {attempt}", flush=True)
        break

    if payload is None:
        print(
            f"feed_loop: line {line_id} — gave up after {MAX_FORMAT_ATTEMPTS} attempts (lost cause)",
            flush=True,
        )
        _tally_inc(contact_slug, "lines_format_failed")
        return
    if payload.get("skip"):
        print(f"feed_loop: line {line_id} — model skipped: {payload.get('reason')}", flush=True)
        _tally_inc(contact_slug, "lines_model_skipped")
        return
    # Direct-route fork — model decided this synthesis output is worth
    # sending to the contact as a DM rather than publishing as a feed
    # card. Body is the message text; other card fields (title, image,
    # links) are ignored on this path.
    if payload.get("direct"):
        body = str(payload.get("body") or "").strip()
        if not body:
            print(
                f"feed_loop: line {line_id} — direct=true but body empty; skipping",
                flush=True,
            )
            _tally_inc(contact_slug, "lines_direct_empty")
            return
        try:
            await messages_mod.send_message(
                recipient_slug=contact_slug,
                body=body,
                writer_slug="fathom",
            )
            print(
                f"feed_loop[{contact_slug}]: line {line_id} routed as direct message ({len(body)} chars)",
                flush=True,
            )
            _tally_inc(contact_slug, "lines_direct_sent")
        except Exception:
            log.exception("feed_loop: line %s direct send failed", line_id)
        return
    if not payload.get("title") or not payload.get("body"):
        print(
            f"feed_loop: line {line_id} — JSON valid but missing title/body; skipping. payload keys: {list(payload.keys())}",
            flush=True,
        )
        _tally_inc(contact_slug, "lines_missing_fields")
        return

    # Judge + router stage. The judge rates the card on five axes
    # without seeing the routing rules; the router maps axes → level
    # (or DROP). Architecturally separated so the judge cannot
    # calibrate toward "stay surfaced" — see api/_feed_judge.py.
    from . import _feed_judge, _feed_router  # lazy — avoid bootstrap cycles

    axes = await _feed_judge.judge(payload, contact_slug, kind=kind)
    level = _feed_router.route(kind, axes)
    if level is None:
        print(
            f"feed_loop: line {line_id} dropped by router (kind={kind}, axes={axes})",
            flush=True,
        )
        _tally_inc(contact_slug, "dropped_by_router")
        return

    valid_hashes = _candidate_hashes(candidates)
    valid_urls = _candidate_image_urls(candidates)
    raw_body_image = str(payload.get("body_image", "") or "")
    body_image = _validate_body_image(raw_body_image, valid_hashes, valid_urls)
    if raw_body_image and not body_image:
        print(
            f"feed_loop: line {line_id} dropped hallucinated body_image={raw_body_image!r}",
            flush=True,
        )
    raw_media = [str(m) for m in (payload.get("media") or []) if m]
    media = _validate_media_list(raw_media, valid_hashes, valid_urls)
    if len(raw_media) != len(media):
        print(
            f"feed_loop: line {line_id} dropped {len(raw_media) - len(media)} hallucinated media entr(ies)",
            flush=True,
        )

    # Links: only http(s) URLs. The model could in principle invent a URL,
    # but unlike media_hash we can't validate against a candidate set —
    # links can legitimately come from web search. The http(s) shape is
    # the only floor we enforce; everything else is on the model.
    raw_link = str(payload.get("link", "") or "").strip()
    link = raw_link if raw_link.startswith(("http://", "https://")) else ""
    raw_links = payload.get("links") or []
    links: list[dict] = []
    for entry in raw_links:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url", "") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        title = str(entry.get("title", "") or "").strip()[:120]
        links.append({"title": title, "url": url})

    card = {
        "title": str(payload.get("title", ""))[:200],
        "body": str(payload.get("body", "")),
        "tail": str(payload.get("tail", "") or ""),
        "body_image": body_image,
        "body_image_layout": payload.get("body_image_layout") or "hero",
        "media": media,
        "link": link,
        "links": links,
    }
    tags = [
        CARD_TAG,
        "feed-story",  # back-compat with existing UI reader
        _contact_tag(contact_slug),
        f"kind:{kind}",
        f"level:{level}",
    ]
    for axis_name, axis_value in axes.items():
        tags.append(f"axis:{axis_name}:{round(axis_value, 3)}")
    if line and line.get("id"):
        tags.append(f"directive-line:{line['id']}")
    if line and line.get("topic"):
        tags.append(f"topic:{line['topic']}")
    if crystal and crystal.get("id"):
        tags.append(f"crystal:{crystal['id']}")
    try:
        await delta_client.write(
            content=json.dumps(card, ensure_ascii=False),
            tags=tags,
            source=CARD_SOURCE,
        )
        _tally_inc(contact_slug, "cards_written")
        # ALERT pierces — also DM the contact so a user who isn't
        # watching the dashboard still gets the signal.
        if level == "ALERT":
            await _send_alert_dm(contact_slug, card, kind)
    except Exception:
        log.exception("feed_loop: card delta write failed")


async def _produce_cards(
    contact_slug: str,
    kind: str,
    directive: str,
    candidates: list[dict],
    crystal: dict | None,
    max_rounds: int,
    max_cards: int = 5,
) -> int:
    """Multi-card sibling to _produce_card. Handles the 0-N output shape
    used by drift + volunteered passes.

    Model is expected to return `{"cards": [ ... ]}` — possibly empty, which
    is a valid silent outcome. Each card entry is validated the same way
    per-line cards are (body_image/media hash-or-candidate-URL, link http(s)
    shape) and written as its own feed-card delta.

    `kind` is "drift" or "volunteered"; it ends up in the card tags
    (`drift` / `volunteered`) so the UI can distinguish them and the
    engagement scorer can treat them differently from slotted cards.

    Returns the number of cards actually written.
    """
    from .server import fathom_think  # lazy — avoid circular import

    tally_written = f"{kind}_cards_written"
    tally_silent = f"{kind}_silent"
    tally_format = f"{kind}_format_failed"
    tally_direct_sent = f"{kind}_direct_sent"

    # Same prelude as _produce_card — DM cadence + recent feed cards.
    # Drift and volunteered passes especially benefit from the feed
    # block: they generate 0-N cards from a wide candidate pool, and
    # without seeing what's already been published they tend to circle
    # the same anchors across runs.
    dm_block = await messages_mod.dm_context_block(contact_slug)
    feed_block = await _recent_feed_cards_block(contact_slug)
    prelude = "\n\n".join(b for b in (dm_block, feed_block) if b)
    if prelude:
        directive = prelude + "\n\n" + directive

    user_message = (
        "Produce the cards for the pass described above. Respond with the JSON "
        '{"cards": [...]} object only.'
    )
    last_failed_excerpt: str | None = None
    payload: dict | None = None

    for attempt in range(1, MAX_FORMAT_ATTEMPTS + 1):
        if attempt > 1 and last_failed_excerpt is not None:
            nudge = (
                f"⚠ Your previous attempt was not valid JSON. The output started with:\n"
                f"---\n{last_failed_excerpt}\n---\n\n"
                f"Attempt {attempt} of {MAX_FORMAT_ATTEMPTS}. Respond with ONLY the "
                f'JSON `{{"cards": [...]}}` object. No prose, no markdown fences.'
            )
            this_message = nudge + "\n\n" + user_message
            this_max_rounds = max(2, max_rounds // 3)
        else:
            this_message = user_message
            this_max_rounds = max_rounds

        messages = await fathom_think(
            user_message=this_message,
            directive=directive,
            recall=False,
            max_rounds=this_max_rounds,
        )
        last = messages[-1] if messages else {}
        text = (last.get("content") or "").strip()
        if not text:
            last_failed_excerpt = "(empty message)"
            continue

        candidate_payload = _parse_card_payload(text)
        if candidate_payload is None:
            print(
                f"feed_loop[{kind}]: attempt {attempt} — non-JSON; will retry. excerpt: {text[:200]!r}",
                flush=True,
            )
            last_failed_excerpt = text[:240].replace("\n", " ")
            continue

        payload = candidate_payload
        if attempt > 1:
            print(f"feed_loop[{kind}]: recovered on attempt {attempt}", flush=True)
        break

    if payload is None:
        print(f"feed_loop[{kind}]: gave up after {MAX_FORMAT_ATTEMPTS} attempts", flush=True)
        _tally_inc(contact_slug, tally_format)
        return 0

    cards_list = payload.get("cards")
    if not isinstance(cards_list, list):
        # Model ignored the wrapper. Treat as format failure.
        print(
            f"feed_loop[{kind}]: payload lacked 'cards' array; keys={list(payload.keys())}",
            flush=True,
        )
        _tally_inc(contact_slug, tally_format)
        return 0

    if len(cards_list) == 0:
        reason = (payload.get("reason") or "").strip()[:200]
        print(
            f"feed_loop[{kind}]: model returned empty cards list — silent. reason: {reason!r}",
            flush=True,
        )
        _tally_inc(contact_slug, tally_silent)
        return 0

    # Per-kind cap. Caller passes max_cards from settings (alert: 5,
    # reflection: 2, bridging: 2, discrepancy: 1, drift/volunteered: 5).
    # Defends against the model returning more than its directive asked
    # for; the judge+router still gates each individually below.
    if len(cards_list) > max_cards:
        print(
            f"feed_loop[{kind}]: model returned {len(cards_list)} cards; clipping to {max_cards}",
            flush=True,
        )
        cards_list = cards_list[:max_cards]

    valid_hashes = _candidate_hashes(candidates)
    valid_urls = _candidate_image_urls(candidates)

    written = 0
    for idx, entry in enumerate(cards_list):
        if not isinstance(entry, dict):
            continue
        # Direct-route fork — entry-level. The pass can mix feed cards
        # and direct messages in the same response; each entry decides.
        if entry.get("direct"):
            body = str(entry.get("body") or "").strip()
            if not body:
                print(
                    f"feed_loop[{kind}]: card {idx} direct=true with empty body; skipping",
                    flush=True,
                )
                continue
            try:
                await messages_mod.send_message(
                    recipient_slug=contact_slug,
                    body=body,
                    writer_slug="fathom",
                )
                print(
                    f"feed_loop[{kind}]: card {idx} routed as direct message ({len(body)} chars)",
                    flush=True,
                )
                _tally_inc(contact_slug, tally_direct_sent)
            except Exception:
                log.exception("feed_loop: %s card %d direct send failed", kind, idx)
            continue
        if not entry.get("title") or not entry.get("body"):
            print(
                f"feed_loop[{kind}]: card {idx} missing title/body; skipping. keys={list(entry.keys())}",
                flush=True,
            )
            continue

        # Judge + router stage. Same independent-LLM scoring as the
        # per-line path; router maps axes → level (or DROP).
        from . import _feed_judge, _feed_router  # lazy — avoid bootstrap cycles

        axes = await _feed_judge.judge(entry, contact_slug, kind=kind)
        level = _feed_router.route(kind, axes)
        if level is None:
            print(
                f"feed_loop[{kind}]: card {idx} dropped by router (axes={axes})",
                flush=True,
            )
            _tally_inc(contact_slug, "dropped_by_router")
            continue

        raw_body_image = str(entry.get("body_image", "") or "")
        body_image = _validate_body_image(raw_body_image, valid_hashes, valid_urls)
        if raw_body_image and not body_image:
            print(
                f"feed_loop[{kind}]: card {idx} dropped hallucinated body_image={raw_body_image!r}",
                flush=True,
            )
        raw_media = [str(m) for m in (entry.get("media") or []) if m]
        media = _validate_media_list(raw_media, valid_hashes, valid_urls)

        raw_link = str(entry.get("link", "") or "").strip()
        link = raw_link if raw_link.startswith(("http://", "https://")) else ""
        raw_links = entry.get("links") or []
        links: list[dict] = []
        for lk in raw_links:
            if not isinstance(lk, dict):
                continue
            url = str(lk.get("url", "") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            title_ = str(lk.get("title", "") or "").strip()[:120]
            links.append({"title": title_, "url": url})

        card = {
            "kicker": str(entry.get("kicker", "") or "")[:80],
            "title": str(entry.get("title", ""))[:200],
            "body": str(entry.get("body", "")),
            "tail": str(entry.get("tail", "") or ""),
            "body_image": body_image,
            "body_image_layout": entry.get("body_image_layout") or "hero",
            "media": media,
            "link": link,
            "links": links,
        }
        tags = [
            CARD_TAG,
            "feed-story",  # back-compat with existing UI reader
            kind,  # legacy bare-kind tag — kept for back-compat
            f"kind:{kind}",
            f"level:{level}",
            _contact_tag(contact_slug),
        ]
        for axis_name, axis_value in axes.items():
            tags.append(f"axis:{axis_name}:{round(axis_value, 3)}")
        if crystal and crystal.get("id"):
            tags.append(f"crystal:{crystal['id']}")
        try:
            await delta_client.write(
                content=json.dumps(card, ensure_ascii=False),
                tags=tags,
                source=CARD_SOURCE,
            )
            _tally_inc(contact_slug, tally_written)
            written += 1
            # ALERT pierces — also DM the contact so the signal reaches
            # them whether or not they're watching the dashboard.
            if level == "ALERT":
                await _send_alert_dm(contact_slug, card, kind)
        except Exception:
            log.exception("feed_loop: %s card delta write failed (idx=%d)", kind, idx)
    return written


async def _fire_drift(contact_slug: str, crystal: dict | None) -> None:
    """Run one drift pass — the free-association slot.

    Assembles the now-anchor, pulls a scatter of old content-bearing deltas,
    hands both to the model with the drift directive, writes 0-5 cards.
    """
    from ._feed_drift import (
        anchor_now_text,
        build_drift_directive,
        fetch_drift_candidates,
        format_drift_pool,
    )

    anchor_text = await anchor_now_text(contact_slug)
    candidates = await fetch_drift_candidates(limit=20)
    print(f"feed_loop[drift]: candidates pre-fetched: {len(candidates)}", flush=True)

    # Drift is allowed to run against a small pool — even 2-3 old deltas can
    # be enough for a resonance. But if the lake is genuinely empty of old
    # content (fresh install), silence is the right outcome.
    if len(candidates) < 2:
        print("feed_loop[drift]: <2 candidates; skipping", flush=True)
        _tally_inc(contact_slug, "drift_silent")
        return

    directive = build_drift_directive(anchor_text, format_drift_pool(candidates))

    _llm_active_enter(contact_slug, label="Drift pass — free association")
    try:
        await asyncio.wait_for(
            _produce_cards(
                contact_slug,
                kind="drift",
                directive=directive,
                candidates=candidates,
                crystal=crystal,
                max_rounds=settings.feed_drift_budget_tool_calls,
            ),
            timeout=settings.feed_loop_budget_seconds,
        )
    except TimeoutError:
        print("feed_loop[drift]: timed out", flush=True)
        _tally_inc(contact_slug, "drift_timed_out")
    finally:
        _llm_active_exit(contact_slug)


async def _fire_volunteered(contact_slug: str, crystal: dict | None) -> None:
    """Run one volunteered-noticing pass — the present-salience slot."""
    from ._feed_volunteer import (
        anchor_crystal_context,
        build_volunteer_directive,
        fetch_volunteer_candidates,
        format_volunteer_pool,
    )

    crystal_context = await anchor_crystal_context(contact_slug)
    candidates = await fetch_volunteer_candidates(limit=20)
    print(f"feed_loop[volunteered]: candidates pre-fetched: {len(candidates)}", flush=True)

    # Volunteered leans on the notion that "the day has been ordinary" is
    # a valid outcome — fewer candidates means less salience to notice, not
    # a skip. 1 is the floor, same as the per-line pass.
    if len(candidates) < 1:
        print("feed_loop[volunteered]: no candidates; skipping", flush=True)
        _tally_inc(contact_slug, "volunteered_silent")
        return

    directive = build_volunteer_directive(crystal_context, format_volunteer_pool(candidates))

    _llm_active_enter(contact_slug, label="Noticing what stood out today")
    try:
        await asyncio.wait_for(
            _produce_cards(
                contact_slug,
                kind="volunteered",
                directive=directive,
                candidates=candidates,
                crystal=crystal,
                max_rounds=settings.feed_loop_budget_tool_calls,
            ),
            timeout=settings.feed_loop_budget_seconds,
        )
    except TimeoutError:
        print("feed_loop[volunteered]: timed out", flush=True)
        _tally_inc(contact_slug, "volunteered_timed_out")
    finally:
        _llm_active_exit(contact_slug)


# ── New synthesis-rebuild passes ─────────────────────────────────────────
#
# Alert / reflection / bridging / discrepancy. Same shape as drift +
# volunteered: assemble pass-specific context, fetch candidates, hand
# both to _produce_cards via the kind-tagged directive. Each pass
# self-silences when nothing meaningful surfaces — the empty-cards
# response is healthy. Per-kind caps live in settings.
#
# All four run regardless of crystal state, before the per-line/drift/
# volunteered passes — the alert pass especially needs to fire even on
# a bare-install lake where no crystal has formed yet.


async def _fire_alert(contact_slug: str, crystal: dict | None) -> None:
    """Alert pass — piercing tier. Looks for things outside the normal
    pattern of the lake right now."""
    from ._feed_alert import (
        build_alert_directive,
        fetch_baseline_window,
        fetch_recent_window,
        format_recent_for_alert,
        _format_source_counts,
    )

    recent = await fetch_recent_window()
    baseline = await fetch_baseline_window()
    print(
        f"feed_loop[alert]: recent={len(recent)}, baseline={len(baseline)}",
        flush=True,
    )

    if not recent:
        # Nothing recent to scan. Truly quiet lake. Silence is correct.
        print("feed_loop[alert]: empty recent window; skipping", flush=True)
        _tally_inc(contact_slug, "alert_silent")
        return

    directive = build_alert_directive(
        recent_window_text=format_recent_for_alert(recent),
        recent_summary=_format_source_counts(recent),
        baseline_summary=_format_source_counts(baseline),
    )

    _llm_active_enter(contact_slug, label="Alert pass — scanning for deviations")
    try:
        await asyncio.wait_for(
            _produce_cards(
                contact_slug,
                kind="alert",
                directive=directive,
                candidates=recent,
                crystal=crystal,
                max_rounds=settings.feed_loop_budget_tool_calls,
                max_cards=settings.feed_pass_budget_alert,
            ),
            timeout=settings.feed_loop_budget_seconds,
        )
    except TimeoutError:
        print("feed_loop[alert]: timed out", flush=True)
        _tally_inc(contact_slug, "alert_timed_out")
    finally:
        _llm_active_exit(contact_slug)


async def _fire_reflection(contact_slug: str, crystal: dict | None) -> None:
    """Reflection pass — provenance / wisdom-as-sediment generation."""
    from ._feed_reflection import (
        build_reflection_directive,
        fetch_recent_reflections,
        fetch_reflection_candidates,
        format_activity_pool,
        format_recent_reflections,
    )

    activity = await fetch_reflection_candidates()
    print(f"feed_loop[reflection]: activity={len(activity)}", flush=True)

    if len(activity) < 3:
        print("feed_loop[reflection]: <3 reflectable items; skipping", flush=True)
        _tally_inc(contact_slug, "reflection_silent")
        return

    recent_reflections = await fetch_recent_reflections()
    directive = build_reflection_directive(
        activity_pool_text=format_activity_pool(activity),
        recent_reflections_text=format_recent_reflections(recent_reflections),
    )

    _llm_active_enter(contact_slug, label="Reflection pass — sediment of recent work")
    try:
        await asyncio.wait_for(
            _produce_cards(
                contact_slug,
                kind="reflection",
                directive=directive,
                candidates=activity,
                crystal=crystal,
                max_rounds=settings.feed_loop_budget_tool_calls,
                max_cards=settings.feed_pass_budget_reflection,
            ),
            timeout=settings.feed_loop_budget_seconds,
        )
    except TimeoutError:
        print("feed_loop[reflection]: timed out", flush=True)
        _tally_inc(contact_slug, "reflection_timed_out")
    finally:
        _llm_active_exit(contact_slug)


async def _fire_bridging(contact_slug: str, crystal: dict | None) -> None:
    """Bridging pass — cross-workspace structural pattern matching."""
    from ._feed_bridging import (
        build_bridging_directive,
        fetch_deep_pool,
        fetch_recent_by_source,
        format_deep_pool,
        format_recent_by_source,
    )

    recent_by_source = await fetch_recent_by_source()
    deep_pool = await fetch_deep_pool()
    print(
        f"feed_loop[bridging]: recent_sources={len(recent_by_source)}, deep={len(deep_pool)}",
        flush=True,
    )

    # Bridging needs material on both sides of the bridge.
    if len(recent_by_source) < 2 and len(deep_pool) < 5:
        print("feed_loop[bridging]: insufficient material to bridge; skipping", flush=True)
        _tally_inc(contact_slug, "bridging_silent")
        return

    directive = build_bridging_directive(
        recent_by_source=format_recent_by_source(recent_by_source),
        deep_pool=format_deep_pool(deep_pool),
    )

    # Flatten recent_by_source to a single list for candidate validation.
    flat_recent: list[dict] = []
    for items in recent_by_source.values():
        flat_recent.extend(items)
    candidates = flat_recent + deep_pool

    _llm_active_enter(contact_slug, label="Bridging pass — cross-workspace echoes")
    try:
        await asyncio.wait_for(
            _produce_cards(
                contact_slug,
                kind="bridging",
                directive=directive,
                candidates=candidates,
                crystal=crystal,
                max_rounds=settings.feed_loop_budget_tool_calls,
                max_cards=settings.feed_pass_budget_bridging,
            ),
            timeout=settings.feed_loop_budget_seconds,
        )
    except TimeoutError:
        print("feed_loop[bridging]: timed out", flush=True)
        _tally_inc(contact_slug, "bridging_timed_out")
    finally:
        _llm_active_exit(contact_slug)


async def _fire_discrepancy(contact_slug: str, crystal: dict | None) -> None:
    """Discrepancy pass — internal-contradiction detection (the
    uncomfortable-truth lane that prevents flattery drift)."""
    from ._feed_discrepancy import (
        build_discrepancy_directive,
        fetch_older_user_positions,
        fetch_recent_user_positions,
        format_user_positions,
    )

    recent = await fetch_recent_user_positions()
    older = await fetch_older_user_positions()
    print(
        f"feed_loop[discrepancy]: recent={len(recent)}, older={len(older)}",
        flush=True,
    )

    # Need genuine corpus on both sides.
    if len(recent) < 3 or len(older) < 3:
        print(
            "feed_loop[discrepancy]: not enough user positions to compare; skipping",
            flush=True,
        )
        _tally_inc(contact_slug, "discrepancy_silent")
        return

    directive = build_discrepancy_directive(
        recent_text=format_user_positions(recent, ""),
        older_text=format_user_positions(older, ""),
    )

    _llm_active_enter(contact_slug, label="Discrepancy pass — checking for moved positions")
    try:
        await asyncio.wait_for(
            _produce_cards(
                contact_slug,
                kind="discrepancy",
                directive=directive,
                candidates=recent + older,
                crystal=crystal,
                max_rounds=settings.feed_loop_budget_tool_calls,
                max_cards=settings.feed_pass_budget_discrepancy,
            ),
            timeout=settings.feed_loop_budget_seconds,
        )
    except TimeoutError:
        print("feed_loop[discrepancy]: timed out", flush=True)
        _tally_inc(contact_slug, "discrepancy_timed_out")
    finally:
        _llm_active_exit(contact_slug)


async def _has_fresh_card(contact_slug: str, line_id: str, freshness_hours: float) -> bool:
    """True if this contact already has a card for this line newer than the window.

    Per-line fallback. Preferred path is _latest_card_by_line + the map-
    based _is_fresh_from_map — one query per run instead of per line.
    """
    try:
        results = await delta_client.query(
            tags_include=[CARD_TAG, f"directive-line:{line_id}", _contact_tag(contact_slug)],
            limit=1,
        )
    except Exception:
        return False
    if not results:
        return False
    ts = results[0].get("timestamp") or ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return False
    age = _now() - dt
    return age < timedelta(hours=freshness_hours)


async def _latest_card_by_line(contact_slug: str) -> dict[str, str]:
    """Batch-prefetch the latest card timestamp per directive line.

    Single lake query for all cards this contact has, grouped in Python
    by `directive-line:<id>` tag. Returns {line_id: newest_iso_ts}.
    Empty dict on error, which makes every line "stale" — safe default,
    slows down to "regenerate everything this run" rather than missing a
    real freshness skip.

    Limit 200 is comfortably more than any realistic crystal × recent
    fires product; if a contact has more than 200 outstanding cards, the
    oldest ones won't be in the map, which just means they get
    regenerated — no staleness bug, just a wasted regen.
    """
    try:
        cards = await delta_client.query(
            tags_include=[CARD_TAG, _contact_tag(contact_slug)],
            limit=200,
        )
    except Exception:
        return {}
    latest: dict[str, str] = {}
    for c in cards:
        ts = c.get("timestamp") or ""
        for t in c.get("tags") or []:
            if isinstance(t, str) and t.startswith("directive-line:"):
                line_id = t[len("directive-line:") :]
                prev = latest.get(line_id)
                if prev is None or ts > prev:
                    latest[line_id] = ts
                break
    return latest


def _is_fresh_from_map(freshness_map: dict[str, str], line_id: str, freshness_hours: float) -> bool:
    """Shared is-fresh predicate for the map-based path."""
    ts = freshness_map.get(line_id)
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return False
    return (_now() - dt) < timedelta(hours=freshness_hours)


def _alert_dm_body(card: dict, kind: str) -> str:
    """Format an ALERT-level card as a chat DM body.

    The ALERT level is *piercing* by definition — if the user isn't
    actively watching the feed, the alert needs to reach them another
    way. We mirror the card content into a direct message: short,
    legible at a glance, with the link inline if there is one.
    """
    title = (card.get("title") or "").strip()
    body = (card.get("body") or "").strip()
    tail = (card.get("tail") or "").strip()
    link = (card.get("link") or "").strip()
    label = "Alert" if kind == "alert" else kind.capitalize()
    parts: list[str] = []
    if title:
        parts.append(f"⚠ {label} · {title}")
    elif body:
        parts.append(f"⚠ {label}")
    if body:
        parts.append(body)
    if tail:
        parts.append(f"— {tail}")
    if link.startswith(("http://", "https://")):
        parts.append(link)
    return "\n\n".join(p for p in parts if p)


async def _send_alert_dm(contact_slug: str, card: dict, kind: str) -> None:
    """Best-effort DM send for an ALERT-level card. The card is already
    on the feed by this point — a failure here means the user only sees
    it on the dashboard, not in chat. Logged but never raised, so a
    chat-listener hiccup never blocks card production."""
    body = _alert_dm_body(card, kind)
    if not body:
        return
    try:
        await messages_mod.send_message(
            recipient_slug=contact_slug,
            body=body,
            writer_slug="fathom",
        )
        print(
            f"feed_loop[{contact_slug}]: ALERT-level {kind} card mirrored as DM ({len(body)} chars)",
            flush=True,
        )
    except Exception:
        log.exception("feed_loop: ALERT DM send failed (kind=%s)", kind)
