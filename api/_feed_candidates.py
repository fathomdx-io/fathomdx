"""Feed-card candidate pool — gather + format helpers.

Pulled out of api/feed_loop.py so the loop core fits under the
800-line sanity ceiling. Everything here is pure-function read
logic: fetch candidates for a directive line, extract image/link
URLs from content, format a compact pool for the model prompt.
"""

from __future__ import annotations

import asyncio
import re

from . import delta_client


async def _fetch_line_candidates(line: dict, limit: int = 20) -> list[dict]:
    """Pre-fetch a candidate pool for a directive line.

    The model's semantic-search-on-topic was missing relevant content
    (e.g. searching "clever science humor" doesn't surface a Quanta
    article titled "Wonder All Around Us"). Pulling candidates by tag
    + recency + image-bearing tells the model "here are concrete deltas
    that fit this slot — pick from them, don't go fishing."

    Strategies, deduplicated and merged:
      1. Engagement-anchored: deltas tagged `topic:<line_topic>` (the
         crystal's own taxonomy, present once engagement has built up).
      2. Visually-rich recents: rss + browser-extension deltas with
         media_hash or inline markdown images.
      3. Topic semantic search via the lake's /search endpoint.

    Returns newest-first, capped at `limit`.
    """
    topic = (line.get("topic") or "").strip()
    line_id = (line.get("id") or "").strip()

    # Fire every lake read for this card's pool in parallel. They're
    # independent — four sequential round-trips against delta-store
    # add up to ~1-2s at typical compose-stack latency, vs. ~500ms when
    # run concurrently. asyncio.gather + return_exceptions=True keeps
    # one failed fetch from breaking the others, matching the per-try
    # except-pass shape the old serial code had.
    semantic_query = f"{topic} {line_id}".replace("-", " ").strip() if (topic or line_id) else ""
    topic_task = delta_client.query(tags_include=[f"topic:{topic}"], limit=limit) if topic else None
    rss_task = delta_client.query(tags_include=["rss"], limit=1000)
    ext_task = delta_client.query(tags_include=["browser-extension"], limit=15)
    search_task = delta_client.search(query=semantic_query, limit=limit) if semantic_query else None
    tasks = [t for t in (topic_task, rss_task, ext_task, search_task) if t is not None]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Pull the results back out in the same order we queued them.
    ri = iter(results)
    topic_res = next(ri) if topic_task is not None else None
    rss_res = next(ri)
    ext_res = next(ri)
    search_res = next(ri) if search_task is not None else None

    seen: set[str] = set()
    pool: list[dict] = []

    def _add(d: dict) -> None:
        did = d.get("id")
        if did and did not in seen:
            seen.add(did)
            pool.append(d)

    # 1. Topic-tagged content
    if isinstance(topic_res, list):
        for d in topic_res:
            _add(d)

    # 2. Visually-rich recent deltas (rss + browser-extension).
    # The rss source plugin creates two delta families per item: the rich
    # digest delta (source like `rss/<source-id>`, content 1000+ chars with
    # markdown image AND `[Source](url)` link) AND a thin upload-sidecar
    # delta (source=`rss`, content="<title>" only, ≤100 chars). Both share
    # the media_hash; both are tagged `rss`+`feed`. Only the digest carries
    # `feed:<domain>` — that's our reliable filter.
    # Limit is large because the sidecar uploads dominate recency — each
    # poll cycle creates ~30 sidecars per source, all with fresh
    # write-time timestamps, while the rich digest deltas use the
    # article's pubDate (often yesterday or older). 1000 is enough to
    # reach a few days of digests on a modest-volume install.
    if isinstance(rss_res, list):
        for d in rss_res:
            tags = d.get("tags") or []
            content = d.get("content") or ""
            # Keep only digest deltas: distinguished by a `feed:<domain>` tag.
            # The sidecar uploads only carry the bare `feed` tag.
            has_feed_domain = any(isinstance(t, str) and t.startswith("feed:") for t in tags)
            if not has_feed_domain:
                continue
            has_image = bool(d.get("media_hash")) or "![" in content
            if not has_image:
                continue
            _add(d)

    # Browser-extension deltas (Reddit captures, etc.) don't have the
    # sidecar problem — keep the original simple shape.
    if isinstance(ext_res, list):
        for d in ext_res:
            content = d.get("content") or ""
            has_image = bool(d.get("media_hash")) or "![" in content
            if has_image:
                _add(d)

    # 3. Semantic search on the topic + line keywords. Catches near-misses
    # (e.g. line "physics-breakthroughs" surfacing Quanta articles).
    if isinstance(search_res, dict):
        search_results = search_res.get("results")
        if search_results:
            for d in search_results:
                _add(d)

    pool.sort(key=lambda d: d.get("timestamp") or "", reverse=True)
    return pool[:limit]


_MARKDOWN_IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")
# Match a markdown link `[label](url)` that is NOT preceded by `!` (which
# would make it an image). The negative lookbehind keeps image markdown
# from getting double-counted in the link extractor.
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _extract_external_url(content: str) -> str | None:
    """First http(s) markdown image URL in the content, if any.

    Offered as a fallback when a candidate has no media_hash. In-lake
    hashes are preferred — they're stable (external URLs can be signed/
    expiring, like atlasobscura imgproxy) and the UI renders them via
    /v1/media/{hash}?token=... which <img> tags can pass directly.
    """
    if not content:
        return None
    m = _MARKDOWN_IMG_RE.search(content)
    return m.group(1) if m else None


def _extract_source_link(content: str) -> str | None:
    """First markdown link in the content. The RSS source plugin appends
    `[Source](url)` to every item, so this is usually the canonical article
    URL. Other sources may use other labels — the link is the link.
    """
    if not content:
        return None
    m = _MARKDOWN_LINK_RE.search(content)
    return m.group(2) if m else None


def _format_candidates(pool: list[dict]) -> str:
    """Compact candidate listing for the per-line directive."""
    if not pool:
        return "(no candidates pre-fetched — fall back to the search tools)"
    lines = []
    for d in pool[:20]:
        ts = (d.get("timestamp") or "")[:16]
        src = (d.get("source") or "?")[:24]
        did = (d.get("id") or "")[:12]
        media_hash = d.get("media_hash") or ""
        content = (d.get("content") or "").strip().split("\n", 1)[0][:140]
        # Surface BOTH media_hashes and external URLs when present, with
        # hashes preferred (the model is told this in the directive). Hashes
        # are stable — external URLs are often imgproxy-signed and expire
        # between RSS poll and render. The UI reaches /v1/media/{hash} with
        # a token query param, so <img> tags render hashes just fine.
        ext_url = _extract_external_url(d.get("content") or "")
        source_link = _extract_source_link(d.get("content") or "")
        # URLs are NOT truncated — the model needs the full string to copy
        # exactly. A truncated URL is worse than no URL: the model assumes
        # what it sees is complete and ships a broken image / dead link.
        marks = []
        if media_hash:
            marks.append(f"📷[hash={media_hash}]")
        if ext_url:
            marks.append(f"🖼[url={ext_url}]")
        if source_link:
            marks.append(f"🔗[link={source_link}]")
        mark = " ".join(marks) if marks else "  "
        lines.append(f"  {mark} [{ts}] {src:24s} ({did}) {content}")
    return "\n".join(lines)
