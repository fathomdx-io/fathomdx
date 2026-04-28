"""Vampire tap — lake → puddle resonance mirror.

The witness's `anchors_block` reads identity facets and the current mood
from the puddle. Without something filling those tags, the witness
writes from voice thoughts alone — correct but flavorless.

This module pulls the latest identity-crystal and latest mood-delta
from the durable lake and writes them as puddle deltas with the same
tag conventions the witness already queries (`crystal` + `facet:<slug>`,
`mood` + `feeling:<state>`).

For v1 this is one-shot at boot + refresh-every-N. The experiment also
mirrored arbitrary substrate activity into the puddle continuously, but
that's a resonance/settle-driven feature we haven't ported yet — adding
it would put noise into the puddle without the metrics that filter it
back out.
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
# vampire-tap pull asks for any new lake delta written in the last
# MIRROR_WINDOW_S seconds; previously-mirrored ids get deduped.
MIRROR_WINDOW_S = 5 * 60

# Sources we never mirror — output-side noise that would echo back into
# the puddle as "new" activity and feed the loop its own footprint.
MIRROR_NOISE_SOURCES = frozenset({
    "fathom-feed",       # legacy feed-card writer
    "loop-engagement",   # the puddle's own promote-to-lake writes
    "controller",        # process spawn/die markers (already in puddle)
    "voice",             # voice-thought writes (already in puddle)
    "witness",           # witness output writes (already in puddle)
    "intent-detector",   # intent writes (already in puddle)
    "composer",          # composer seed writes (already in puddle)
    "mood-crystal",      # would re-pull our own anchor write
    "crystal",           # ditto
})

_mirrored_lake_ids: set[str] = set()


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
        print(f"[vampire] crystal fetch failed: {type(e).__name__}: {e}")
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
        print(f"[vampire] mood fetch failed: {type(e).__name__}: {e}")
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
    """Pull recent lake deltas and mirror them into the puddle as
    `lake-delta` items. Mirrors are TTL'd copies; the originals stay
    durable in the lake forever. Sources that are themselves loop-
    output (witness writes, voice thoughts, the puddle's own promotes)
    are filtered out so the loop doesn't echo on its own footprint.

    Idempotent across ticks — `_mirrored_lake_ids` remembers what's
    already landed so the same lake delta isn't re-mirrored each pull.
    Returns the count of mirrors written this tick.
    """
    from datetime import datetime, timedelta, UTC
    cutoff = (datetime.now(UTC) - timedelta(seconds=MIRROR_WINDOW_S)).isoformat()
    try:
        items = await delta_client.query(time_start=cutoff, limit=200)
    except Exception as e:
        print(f"[vampire] activity fetch failed: {type(e).__name__}: {e}")
        return 0

    written = 0
    for d in items:
        did = d.get("id") or ""
        if not did or did in _mirrored_lake_ids:
            continue
        src = (d.get("source") or "").strip()
        if src in MIRROR_NOISE_SOURCES:
            _mirrored_lake_ids.add(did)
            continue
        content = (d.get("content") or "").strip()
        if not content or len(content) < 8:
            _mirrored_lake_ids.add(did)
            continue
        # Skip echoes — anything that already landed in the convo via
        # another path (rare, but possible if a contact slug overlap
        # makes the contact-tag scope a delta into our convo).
        src_tags = d.get("tags") or []
        if any(t == CONVO_TAG for t in src_tags):
            _mirrored_lake_ids.add(did)
            continue

        await puddle.write(
            content=content,
            tags=[
                CONVO_TAG,
                "mirror",
                "lake-delta",
                f"from-source:{src or 'unknown'}",
                f"recalled-id:{did[:24]}",
            ],
            source=f"mirror:{src or 'unknown'}",
            ttl_seconds=ANCHOR_TTL_S,
        )
        _mirrored_lake_ids.add(did)
        written += 1
    return written


async def refresh_anchors() -> None:
    """One refresh pass — pull crystal + mood. Used on boot and on
    interval. Logs but never raises; a transient lake hiccup must not
    take down the loop."""
    try:
        n = await pull_crystal()
        if n:
            print(f"[vampire] refreshed {n} crystal facet(s) in the puddle")
    except Exception as e:
        print(f"[vampire] crystal refresh crashed: {type(e).__name__}: {e}")
    try:
        if await pull_mood():
            print("[vampire] refreshed mood in the puddle")
    except Exception as e:
        print(f"[vampire] mood refresh crashed: {type(e).__name__}: {e}")
    try:
        n = await mirror_recent_activity()
        if n:
            print(f"[vampire] mirrored {n} new lake delta(s) into the puddle")
    except Exception as e:
        print(f"[vampire] activity mirror crashed: {type(e).__name__}: {e}")


async def vampire_loop() -> None:
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
