"""Telepathy — the puddle's real-time view into the lake.

The puddle observes; it doesn't own. Telepathy is the mechanism that
keeps the puddle aware of what's actually happening in the lake right
now: the witness's identity facets, the latest mood, anything the user
or external sources just authored. Without it the loop would write
from voice thoughts alone — correct, but unmoored from what's
currently true.

Three pulls run every refresh:

  * pull_crystal — split the latest identity-crystal into facet deltas
    so the witness can surface individual facets independently.
  * pull_mood — render the latest mood-delta as a `mood` card so the
    integrated take colors with the felt-sense layer.
  * mirror_recent_activity — copy any lake delta written in the
    MIRROR_WINDOW_S seconds before this tick into the puddle as a
    `lake-delta` item, filtered to drop loop-output sources so the
    loop doesn't echo on its own footprint.

Dedupe truth lives in the puddle itself — every echoed lake delta
carries a `recalled-id:<24chars>` tag (shared convention with
recall.py and the dual-write paths in witness.py / routes.py). The
mirror pass queries the puddle for that tag set up front and skips
any lake delta whose short id is already represented, no matter
which path put it there. Reaping a TTL'd echo cleanly drops the
dedupe entry, so a still-recent lake delta gets re-mirrored after
its earlier copy expires.
"""

from __future__ import annotations

import asyncio
import json
import re

from .. import delta_client
from .intents import CONVO_TAG
from .puddle import puddle


# Crystal + mood TTLs in the puddle. Generous because they're durable
# context — the witness reads them on every fire, and they don't churn.
# Refreshed by the periodic re-pull (REFRESH_INTERVAL_S) so a stale
# mood doesn't anchor the loop after the user's mood has actually moved.
# Matches the rolling 48h horizon every other puddle item uses, since
# the dedupe on identical content keeps steady-state writes quiet —
# the long TTL just means the latest pulled state stays available.
ANCHOR_TTL_S = 48 * 60 * 60  # 48h rolling horizon
REFRESH_INTERVAL_S = 5 * 60  # re-pull every 5 minutes

# Mirror window — how far back to look in the lake on each tick. Each
# telepathy pass asks for the newest deltas written in the last
# MIRROR_WINDOW_S seconds, capped at MIRROR_LIMIT items, whichever
# bound hits first. Previously-mirrored ids get deduped via the
# `recalled-id:` tag, so steady-state writes are quiet — only deltas
# the puddle hasn't seen actually land. The wider 24h window means
# a fresh boot pulls a day of ambient context instead of just the
# last 5 minutes; the limit caps total volume so a busy day doesn't
# flood the puddle.
MIRROR_WINDOW_S = 48 * 60 * 60  # match ANCHOR_TTL_S — anything in the puddle
                                # could've been there for up to 48h, so the
                                # cold-start backfill should reach back as
                                # far as the puddle's own TTL would let it.
MIRROR_LIMIT = 1500             # bumped from 200 — even with heartbeats
                                # excluded server-side, a busy day still
                                # holds ~600 non-heartbeat deltas
                                # (homeassistant + sysinfo + sediment +
                                # rss + claude-code + witness + chat).
                                # 1500 lets the cold-start backfill reach
                                # the full 48h horizon. Memory is fine —
                                # at typical content sizes it's a few MB.

# Sources we never mirror — telepathy's own anchor writes (so we
# don't loop on our own output). composer / witness / loop-engagement
# used to be in here back when those paths were puddle-only; after
# the puddle/lake split they're real authored content, and the
# recalled-id: dedup handles the in-flight echo case so duplicates
# don't appear. Voice thoughts and pressure intents are puddle-only
# by design; their sources are listed defensively in case some
# external path ever writes those names to the lake.
MIRROR_NOISE_SOURCES = frozenset({
    "controller",        # process spawn/die markers (puddle-only)
    "voice",             # voice-thought writes (puddle-only)
    "intent-detector",   # default intent source (puddle-only)
    "mood-crystal",      # would re-pull our own anchor write
    "crystal",           # ditto
    "feed-orient",       # pulled as a facet via pull_feed_orient
})


def _slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len] or "unnamed"


def _parse_crystal_facets(content: str) -> list[tuple[str, str, str]]:
    """Split crystal markdown into (slug, header, body) triples.

    The crystal is structured as `## Header\\n<body>` blocks. Each becomes
    its own puddle delta so the witness can surface individual facets
    independently rather than dumping the whole crystal into the prompt.
    """
    out: list[tuple[str, str, str]] = []
    sections = re.split(r"^##\s+", content, flags=re.MULTILINE)
    for sec in sections[1:]:  # skip preamble before first ##
        lines = sec.split("\n", 1)
        header = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        if header and body:
            out.append((_slug(header), header, body))
    return out


async def pull_crystal() -> int:
    """Pull the latest identity crystal from the lake, write its facets
    to the puddle. Idempotent: skips writes when the puddle already has
    a matching `facet:<slug>` delta with identical content (saves
    pointless puddle churn on every refresh).

    Returns the number of facet deltas written this pass.
    """
    try:
        items = await delta_client.query(
            tags_include=["identity-crystal", "crystal-regen"],
            limit=1,
        )
    except Exception as e:
        print(f"[telepathy] crystal fetch failed: {type(e).__name__}: {e}")
        return 0
    if not items:
        return 0

    crystal = items[0]
    facets = _parse_crystal_facets(crystal.get("content") or "")
    if not facets:
        return 0

    # Index existing crystal facets in the puddle by slug.
    existing_by_slug: dict[str, str] = {}
    for d in puddle.query(tags_include=[CONVO_TAG, "crystal"], limit=100):
        for t in d.get("tags") or []:
            if t.startswith("facet:"):
                slug = t.split(":", 1)[1]
                existing_by_slug.setdefault(slug, (d.get("content") or "").strip())
                break

    written = 0
    for slug, header, body in facets:
        new_content = f"## {header}\n\n{body}".strip()
        if existing_by_slug.get(slug) == new_content:
            continue
        await puddle.write(
            content=new_content,
            tags=[CONVO_TAG, "crystal", f"facet:{slug}"],
            source="crystal",
            ttl_seconds=ANCHOR_TTL_S,
        )
        written += 1
    return written


async def pull_feed_orient() -> bool:
    """Pull the latest `crystal:feed-orient` delta and surface its
    narrative as a `facet:feed-orient` puddle delta so the witness's
    anchors_block reads it alongside the identity crystal facets.

    The lake delta carries the full JSON (narrative + directive_lines
    + topic_weights + skip_rules) per FEED_CRYSTAL_DIRECTIVE. The
    witness only needs the narrative — the rest stays durable in the
    lake for downstream consumers (ranking, audit, future filtering).
    Content-deduped: if the puddle's existing facet:feed-orient text
    matches what we'd write, no-op."""
    try:
        items = await delta_client.query(
            tags_include=["crystal:feed-orient"],
            limit=1,
        )
    except Exception as e:
        print(f"[telepathy] feed-orient fetch failed: {type(e).__name__}: {e}")
        return False
    if not items:
        return False

    delta = items[0]
    try:
        payload = json.loads(delta.get("content") or "{}")
    except Exception:
        return False
    narrative = (payload.get("narrative") or "").strip()
    if not narrative:
        return False
    body = f"## What you want in your feed right now\n\n{narrative}".strip()

    existing = puddle.query(
        tags_include=[CONVO_TAG, "crystal", "facet:feed-orient"],
        limit=1,
    )
    if existing and (existing[0].get("content") or "").strip() == body:
        return False

    await puddle.write(
        content=body,
        tags=[CONVO_TAG, "crystal", "facet:feed-orient"],
        source="crystal",
        ttl_seconds=ANCHOR_TTL_S,
    )
    return True


async def pull_mood() -> bool:
    """Pull the latest mood-delta and write a `mood` card to the puddle.

    Mood is the felt-sense layer — the witness reads it to color the
    integrated take. Only writes when content has actually changed
    (mood updates land cheaply but writing every cycle would just churn
    TTLs without adding signal).
    """
    try:
        items = await delta_client.query(tags_include=["mood-delta"], limit=1)
    except Exception as e:
        print(f"[telepathy] mood fetch failed: {type(e).__name__}: {e}")
        return False
    if not items:
        return False

    mood_delta = items[0]
    try:
        payload = json.loads(mood_delta.get("content") or "{}")
    except Exception:
        return False

    state = (payload.get("state") or "unknown").strip()
    headline = (payload.get("headline") or "").strip()
    subtext = (payload.get("subtext") or "").strip()
    carrier = (payload.get("carrier_wave") or "").strip()

    parts = [f"## Current mood: {state}"]
    if headline:
        parts.append(f"**{headline}**")
    if subtext:
        parts.append(subtext)
    if carrier:
        parts.append(carrier)
    body = "\n\n".join(parts)

    # Dedupe — the most recent mood card in the puddle, if identical, skip.
    existing = puddle.query(tags_include=[CONVO_TAG, "mood"], limit=1)
    if existing and (existing[0].get("content") or "").strip() == body.strip():
        return False

    await puddle.write(
        content=body,
        tags=[CONVO_TAG, "mood", f"feeling:{state}"],
        source="mood-crystal",
        ttl_seconds=ANCHOR_TTL_S,
    )
    return True


async def mirror_recent_activity() -> int:
    """Dump recent lake deltas into the puddle, preserving their tags.

    Telepathy is intentionally dumb: anything in the lake that isn't
    one of telepathy's own anchor writes lands in the puddle with its
    original tags + content + source intact. The renderer dispatches
    on tags downstream, so a `feed-card` lake delta lands tagged
    `feed-card`, a `user-seed` lake delta lands tagged `user-seed`,
    and so on — the shape travels with the delta. We only stamp
    CONVO_TAG (puddle scoping) and `recalled-id:<short>` (the dedup
    contract) on top of whatever was already there.

    Cold-start case: a fresh container boots with an empty puddle.
    Telepathy's first pass mirrors the last 24h of lake activity in
    proper shape, so user seeds, witness cards, and engagement-
    authored deltas all restore as their original kinds.

    Returns the count of mirrors written this tick.
    """
    from datetime import datetime, timedelta, UTC
    cutoff = (datetime.now(UTC) - timedelta(seconds=MIRROR_WINDOW_S)).isoformat()
    try:
        # Exclude heartbeats at the LAKE query level, not after fetch.
        # On a busy day the lake holds ~1500 agent-heartbeat deltas in
        # the last 24h; without server-side exclusion they fill the
        # MIRROR_LIMIT-newest slice and starve the user-facing content
        # (witness cards, composer seeds, chat) the dashboard needs.
        items = await delta_client.query(
            time_start=cutoff,
            tags_exclude=["agent-heartbeat"],
            limit=MIRROR_LIMIT,
        )
    except Exception as e:
        print(f"[telepathy] activity fetch failed: {type(e).__name__}: {e}")
        return 0

    existing_short_ids: set[str] = set()
    for d in puddle.query(tags_include=[CONVO_TAG], limit=2000):
        for t in d.get("tags") or []:
            if t.startswith("recalled-id:"):
                existing_short_ids.add(t.split(":", 1)[1])

    # Lake returns newest-first; we want the puddle's write order to
    # be oldest-first so the newest lake delta gets the latest puddle
    # timestamp. get_feed sorts by puddle timestamp desc, so this is
    # what makes a cold-start dump render newest-first instead of
    # flipping into oldest-first by accident.
    written = 0
    for d in reversed(items):
        did = d.get("id") or ""
        if not did:
            continue
        short = did[:24]
        if short in existing_short_ids:
            continue
        src = (d.get("source") or "").strip()
        if src in MIRROR_NOISE_SOURCES:
            continue
        content = (d.get("content") or "").strip()
        if not content or len(content) < 8:
            continue
        src_tags = list(d.get("tags") or [])
        # Skip agent heartbeats — they're connection signals, not
        # substrate. A busy day produces ~1500 of them, and with
        # MIRROR_LIMIT=200 they crowd out the user-facing content
        # (witness cards, composer seeds, chat) that the dashboard
        # actually wants on cold-start. Filtering by the canonical
        # `agent-heartbeat` tag instead of by source so any future
        # plugin that emits heartbeat-shaped status under a different
        # source still gets caught.
        if "agent-heartbeat" in src_tags:
            continue
        # Defensive: a lake delta carrying CONVO_TAG would mean some
        # other path scoped this delta into our convo; don't re-mirror.
        if any(t == CONVO_TAG for t in src_tags):
            continue

        # Preserve the original tags. Stamp CONVO_TAG (puddle scoping),
        # recalled-id:<short> (the dedup contract), `lake-delta` and
        # `from-source:<src>` (telepathy markers — let the renderer's
        # fallthrough surface anything that didn't match a more
        # specific kind branch as a generic raw-lake row, while
        # specific shapes like feed-card or user-seed still render
        # via their own branches based on the preserved original tags).
        new_tags = src_tags + [
            CONVO_TAG,
            "lake-delta",
            f"from-source:{src or 'unknown'}",
            f"recalled-id:{short}",
        ]

        await puddle.write(
            content=content,
            tags=new_tags,
            source=src or "unknown",
            ttl_seconds=ANCHOR_TTL_S,
        )
        existing_short_ids.add(short)
        written += 1
    return written


async def refresh_anchors() -> None:
    """One refresh pass — pull crystal + mood + feed-orient + recent
    activity. Used on boot and on interval. Logs but never raises; a
    transient lake hiccup must not take down the loop."""
    try:
        n = await pull_crystal()
        if n:
            print(f"[telepathy] refreshed {n} crystal facet(s) in the puddle")
    except Exception as e:
        print(f"[telepathy] crystal refresh crashed: {type(e).__name__}: {e}")
    try:
        if await pull_feed_orient():
            print("[telepathy] refreshed feed-orient facet in the puddle")
    except Exception as e:
        print(f"[telepathy] feed-orient refresh crashed: {type(e).__name__}: {e}")
    try:
        if await pull_mood():
            print("[telepathy] refreshed mood in the puddle")
    except Exception as e:
        print(f"[telepathy] mood refresh crashed: {type(e).__name__}: {e}")
    try:
        n = await mirror_recent_activity()
        if n:
            print(f"[telepathy] mirrored {n} new lake delta(s) into the puddle")
    except Exception as e:
        print(f"[telepathy] activity mirror crashed: {type(e).__name__}: {e}")


async def telepathy_loop() -> None:
    """Background task — periodic refresh forever."""
    await refresh_anchors()  # one immediate pull at boot
    while True:
        try:
            await asyncio.sleep(REFRESH_INTERVAL_S)
        except asyncio.CancelledError:
            return
        try:
            await refresh_anchors()
        except asyncio.CancelledError:
            return
