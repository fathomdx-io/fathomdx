"""RSS/Atom feed source producer.

Converts feed HTML to markdown via html-to-markdown, preserving images,
links, tables, and structure. No LLM needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from urllib.parse import urlparse

import feedparser
import httpx

from .base import ProducedDelta, RawItem, SourceProducer, convert_html, extract_images

log = logging.getLogger("source.rss")


class RSSProducer(SourceProducer):
    source_type = "rss"
    display_name = "RSS"
    description = "Feed items converted to markdown deltas"
    version = "0.2.0"
    author = "fathom"
    auth_type = "none"
    schedule_type = "poll"
    default_interval = "30m"
    digestion = "raw"
    default_expiry_days = 30
    expiry_configurable = True

    async def poll(self, config: dict, since: float | None = None) -> list[RawItem]:
        url = config.get("feed", "")
        if not url:
            return []
        items: list[RawItem] = []
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "Fathom/0.1 (RSS source plugin)"},
        ) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                feed = await asyncio.to_thread(feedparser.parse, resp.text)
                for entry in feed.entries:
                    raw_html = self._entry_content(entry)
                    md_content, image_urls = convert_html(raw_html)
                    # Many photography-first feeds (Guardian Photography,
                    # NASA Image of the Day) carry the actual image in
                    # RSS namespace tags — <media:content>, <enclosure>,
                    # <media:thumbnail> — instead of inline <img> in the
                    # body. convert_html only sees the body HTML, so
                    # those feeds end up with image_urls=[] and no
                    # media_hash. Pull from feedparser's parsed shape and
                    # merge in (preserving order, dedup on URL).
                    for ns_url in self._namespace_image_urls(entry):
                        if ns_url not in image_urls:
                            image_urls.append(ns_url)
                    title = getattr(entry, "title", "")
                    media_hash = (
                        await extract_images(image_urls, http_client=client) if image_urls else None
                    )
                    items.append(
                        RawItem(
                            id=self._entry_id(entry, url),
                            content=md_content,
                            timestamp=self._entry_timestamp(entry),
                            title=title,
                            url=getattr(entry, "link", None),
                            image_urls=image_urls,
                            meta={"media_hash": media_hash} if media_hash else {},
                        )
                    )
            except Exception:
                log.warning("Failed to fetch feed %s", url, exc_info=True)
        return items

    def digest(self, item: RawItem, config: dict | None = None) -> ProducedDelta:
        parts = []
        if item.title:
            parts.append(f"# {item.title}")
        if item.content:
            parts.append(item.content)
        if item.url:
            parts.append(f"[Source]({item.url})")
        content = "\n\n".join(parts)

        tags = ["rss", "feed"]
        if item.url:
            domain = urlparse(item.url).netloc
            if domain:
                tags.append(f"feed:{domain}")

        return ProducedDelta(
            content=content,
            tags=tags,
            source="rss",
            timestamp=item.timestamp,
            image_urls=item.image_urls,
            media_hash=item.meta.get("media_hash"),
        )

    def default_tags(self, config: dict) -> list[str]:
        return ["rss", "feed"]

    def validate_config(self, config: dict) -> list[str]:
        feed = config.get("feed")
        if not feed or not isinstance(feed, str):
            return ["'feed' is required (a URL)"]
        if not feed.startswith(("http://", "https://")):
            return [f"Invalid feed URL: {feed}"]
        return []

    def _entry_id(self, entry: object, feed_url: str) -> str:
        raw_id = getattr(entry, "id", None) or getattr(entry, "link", None)
        if raw_id:
            return raw_id
        title = getattr(entry, "title", "")
        return hashlib.sha256(f"{feed_url}:{title}".encode()).hexdigest()[:16]

    def _namespace_image_urls(self, entry: object) -> list[str]:
        """Pull image URLs from RSS namespace tags feedparser exposes:
        <media:content>, <media:thumbnail>, <enclosure>. Filters to
        image MIME types when a `type` is declared; permits anything
        when it isn't (some feeds omit type but the URL is plainly an
        image). Order: media_content, media_thumbnail, enclosures.
        """
        urls: list[str] = []
        seen: set[str] = set()

        def _add(candidate: object) -> None:
            if not isinstance(candidate, dict):
                return
            url = (candidate.get("url") or "").strip()
            if not url or url in seen:
                return
            mime = (candidate.get("type") or "").lower()
            if mime and not mime.startswith("image/"):
                return
            seen.add(url)
            urls.append(url)

        for attr in ("media_content", "media_thumbnail"):
            for item in getattr(entry, attr, None) or []:
                _add(item)
        for item in getattr(entry, "enclosures", None) or []:
            _add(item)
        return urls

    def _entry_content(self, entry: object) -> str:
        if hasattr(entry, "content") and entry.content:
            return entry.content[0].get("value", "")
        return getattr(entry, "summary", getattr(entry, "description", ""))

    def _entry_timestamp(self, entry: object) -> str | None:
        for attr in ("published_parsed", "updated_parsed"):
            parsed = getattr(entry, attr, None)
            if parsed:
                try:
                    dt = datetime(*parsed[:6], tzinfo=UTC)
                    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    pass
        return None
