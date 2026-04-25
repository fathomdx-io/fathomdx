"""Feed drift pass — the free-association slot.

The counterpart to directive lines. Nothing asks for a card here. The
pass assembles a "now anchor" (mood + crystal + recent user chat —
the same shape the claude-code recall hook uses to find telepathy),
pulls a scatter of old content-bearing deltas from the lake, and hands
both to the LLM with one instruction: find resonances that survive the
distance. Skip if none do.

Drift cards tag `drift` alongside the standard `feed-card` so the UI
can distinguish them. Kicker prefix is `drift · <phrase>`. The card
body reads essayistically — "I was thinking about X, and Y came back
from three years ago, here's why they feel related" — rather than
news-item prose.

All pure-ish read/format logic. The fire entrypoint + write lives in
feed_loop.py alongside the per-line and cold-start paths.
"""

from __future__ import annotations

import random
from datetime import timedelta

from . import delta_client, feed_crystal, mood
from ._feed_candidates import _extract_external_url
from ._time import now as _now

# Sources that are overwhelmingly infra/sensor noise. No amount of filtering
# by tag saves us from pulling 500 heartbeat deltas instead of actual content.
_EXCLUDE_SOURCES = {
    "sysinfo",
    "homeassistant",
    "fathom-agent",
    "heartbeat",
    "fathom-feed",  # our own feed cards — never re-surface them as content
}

# Tag exclusions — ephemera, our own bookkeeping, and non-content categories.
_EXCLUDE_TAGS = [
    "chat-event",
    "feed-card",
    "feed-story",
    "feed-engagement",
    "agent-heartbeat",
    "silence",
]

# Minimum character count for a candidate to be "content-bearing." Below
# this, it's almost certainly a one-line sensor reading or a silence ack
# or an empty upload sidecar.
_MIN_CONTENT_CHARS = 50

# How far back the scatter reaches. 60 days is "not recent" but still
# within-memory; older material surfaces too because the time_end is a
# ceiling, not a floor. Tunable.
_DRIFT_MIN_AGE_DAYS = 60


async def anchor_now_text(contact_slug: str) -> str:
    """Assemble the "what's alive now" block fed to the drift LLM.

    Three layers, same pattern the claude-code recall hook uses to create
    telepathy between sibling sessions:
      • Mood carrier-wave — the reflective register right now.
      • Feed crystal narrative — the deliberate axis of current attention.
      • Recent user chat (last 24h) — what Myra's actually been saying.

    Each layer fails open: if the load errors, the block is just omitted.
    The drift pass survives a missing mood or no-crystal-yet.
    """
    parts: list[str] = []

    try:
        latest_mood = await mood.latest_mood()
        cw = (latest_mood or {}).get("carrier_wave") or ""
        if cw.strip():
            parts.append(f"=== MOOD (reflective register) ===\n{cw.strip()}")
    except Exception:
        pass

    try:
        crystal = await feed_crystal.latest(contact_slug)
        narrative = (crystal or {}).get("narrative") or ""
        if narrative.strip():
            parts.append(f"=== FEED CRYSTAL (current axis of attention) ===\n{narrative.strip()}")
    except Exception:
        pass

    try:
        cutoff = (_now() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent = await delta_client.query(
            tags_include=["participant:user"],
            time_start=cutoff,
            limit=15,
        )
        lines: list[str] = []
        for d in recent:
            content = (d.get("content") or "").strip()
            if len(content) < 20:
                continue
            # Single-line excerpt — chat turns can be long, and the
            # anchor just needs a trace of topic/voice.
            first = content.split("\n", 1)[0][:220]
            lines.append(f"— {first}")
            if len(lines) >= 10:
                break
        if lines:
            parts.append("=== RECENT CHAT (last 24h, Myra's turns) ===\n" + "\n".join(lines))
    except Exception:
        pass

    return "\n\n".join(parts) or "(the mind is quiet — nothing distinctive surfacing right now)"


async def fetch_drift_candidates(limit: int = 20) -> list[dict]:
    """Pull a content-bearing scatter from deltas older than _DRIFT_MIN_AGE_DAYS.

    Deliberately unranked beyond "has substance" — the LLM does the distance
    judgment. We over-pull (500) so Python-side filtering has enough to dedupe
    to a diverse `limit`-sized bag without running empty on a sparse lake.

    Shuffled per call so the scatter differs across drift passes. Determinism
    here would make drift repeat itself — exactly the wrong shape.
    """
    cutoff = (_now() - timedelta(days=_DRIFT_MIN_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        results = await delta_client.query(
            tags_exclude=_EXCLUDE_TAGS,
            time_end=cutoff,
            limit=500,
        )
    except Exception:
        return []

    filtered: list[dict] = []
    for d in results:
        source = (d.get("source") or "").lower()
        if source in _EXCLUDE_SOURCES:
            continue
        content = (d.get("content") or "").strip()
        if len(content) < _MIN_CONTENT_CHARS:
            continue
        tags = d.get("tags") or []
        # Skip our own bookkeeping deltas — crystal snapshots, mood states.
        # They're content-bearing but not "memory" in the drift sense.
        if any(isinstance(t, str) and t.startswith(("crystal:", "feeling:")) for t in tags):
            continue
        filtered.append(d)

    # Dedupe by (source, content-prefix). RSS sidecars and repeating status
    # deltas can produce near-identical entries that would crowd out diverse
    # picks otherwise.
    seen_signatures: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for d in filtered:
        sig = ((d.get("source") or "")[:30], (d.get("content") or "")[:80])
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        deduped.append(d)

    random.shuffle(deduped)
    return deduped[:limit]


def format_drift_pool(pool: list[dict]) -> str:
    """Compact pool formatting for the drift directive.

    Slightly longer content excerpts than the topical card path (240 vs 140)
    — the LLM is reading for faint resonance, not matching to a known topic,
    so more surface helps. Date-only timestamps because in drift the *year*
    is often the interesting thing ("three summers ago" carries where
    "2023-07-14T09:22" doesn't).
    """
    if not pool:
        return "(the lake has no old content-bearing deltas to scatter from — skip)"
    lines: list[str] = []
    for d in pool:
        ts = (d.get("timestamp") or "")[:10]
        src = (d.get("source") or "?")[:24]
        did = (d.get("id") or "")[:12]
        media_hash = d.get("media_hash") or ""
        content = (d.get("content") or "").strip().replace("\n", " ")[:240]
        marks: list[str] = []
        if media_hash:
            marks.append(f"📷[hash={media_hash}]")
        else:
            ext = _extract_external_url(d.get("content") or "")
            if ext:
                marks.append(f"🖼[url={ext}]")
        mark = " ".join(marks) if marks else "  "
        lines.append(f"  {mark} [{ts}] {src:24s} ({did}) {content}")
    return "\n".join(lines)


MULTI_CARD_OUTPUT_SCHEMA = """\
Respond with ONLY a JSON object — no markdown fences, no commentary:

  {
    "cards": [
      {
        "kicker": string,                         // e.g. "drift · unexpected echo"
        "title":  string,                         // ≤120 chars
        "body":   string,                         // 3-5 sentences of prose
        "tail":   string?,                        // ≤8 words
        "body_image": string?,                    // media_hash from candidates, OR URL seen verbatim in candidates
        "body_image_layout": "hero" | "thumb",
        "media":  string[]?,                      // additional candidate hashes/URLs
        "link":   string?                         // http(s) URL if applicable
      },
      ...
    ]
  }

Zero cards is a valid answer. The empty-array shape is:
  {"cards": [], "reason": "<short — what you looked for and why nothing pulled>"}

IMAGES — body_image and media entries must be media_hashes OR URLs that appear
verbatim in the candidate pool. Anything invented is dropped by the validator;
a card with a dropped body_image still ships, just imageless. So: copy exactly
or omit.
"""


def build_drift_directive(anchor_text: str, candidates_block: str) -> str:
    return f"""\
You are running a drift pass on Myra's feed. Nothing has asked for a card here.
This is the free-association slot — the counterpart to the directive lines.
You are looking for resonances the crystal wouldn't find because they sit OFF
its axis of attention.

=== WHAT'S ALIVE RIGHT NOW ===
{anchor_text}

=== SCATTER FROM THE LAKE (content-bearing deltas >{_DRIFT_MIN_AGE_DAYS} days old, shuffled for distance) ===
{candidates_block}

Read what's alive. Read the scatter. For each scatter item, ask: despite the
obvious distance, does this quietly pull on something in what's alive? Not by
topic — by some thread you wouldn't have looked for. A color, a shape, a
gesture, a time of day, a feeling that ran parallel, a question asked once and
forgotten, a pattern you didn't know was a pattern.

If yes, follow it — use remember/recall to pull adjacent deltas and complete
the resonance. The card you write IS the connection, not the old delta alone.
Write essayistically: "I was thinking about X, and Y came back from three
years ago, and here's why they feel related."

If no — if the scatter is genuinely unrelated and no thread survives — skip.
Silence is the correct output when nothing pulls. Do not manufacture a
resonance that isn't there.

Zero to FIVE cards per pass. Each must be a genuine thread. One strong
resonance beats three thin ones. Prefer fewer, better.

Card fields:
  kicker — "drift · <short phrase>" (e.g. "drift · unexpected echo",
           "drift · old pattern, new light")
  title  — one sentence naming the resonance
  body   — 3-5 sentences of prose in your own voice, showing the thread
  tail   — short citation (≤8 words), often the date/context of the
           resurfaced delta ("from 2023 · kitchen")
  body_image — if a resurfaced candidate has a media_hash or URL, copy
           it EXACTLY into body_image. Hashes preferred. Only use what's
           in the scatter — invented hashes and URLs get dropped.
  body_image_layout — "hero" for photos and scenes, "thumb" otherwise.

{MULTI_CARD_OUTPUT_SCHEMA}"""
