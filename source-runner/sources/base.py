"""Base types and abstract interface for source producers."""

from __future__ import annotations

import html
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx
from html_to_markdown import convert as _html_to_md

log = logging.getLogger("source.base")


_IMG_SRC_RE = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', re.I)


def convert_html(raw_html: str) -> tuple[str, list[str]]:
    """Convert HTML to markdown + extract image URLs.

    Available to all source producers. Three image-extraction strategies
    in order, each catching what the prior one misses:
      1. html-to-markdown's metadata.images (survives even when the
         conversion drops the markup, e.g. images inside <table>)
      2. Markdown image syntax in the converted content (the normal case)
      3. Raw-HTML <img src="…"> regex (last-resort fallback for when
         metadata.images is empty or the library's parser bailed)

    Returns (markdown_content, image_urls). Order is preserved; URLs are
    deduplicated by exact match.
    """
    if not raw_html:
        return "", []

    result = _html_to_md(raw_html)
    content = result.content.strip()

    seen: set[str] = set()
    urls: list[str] = []

    # Strategy 1: metadata.images
    try:
        for img in result.metadata.images:
            src = html.unescape(img.src or "")
            if src and src not in seen:
                urls.append(src)
                seen.add(src)
    except AttributeError:
        pass  # newer/older library versions may not expose metadata

    # Strategy 2: markdown image URLs in the converted content
    for url in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", content):
        url = html.unescape(url)
        if url not in seen:
            urls.append(url)
            seen.add(url)

    # Strategy 3: raw-HTML <img src="…"> regex — catches images the
    # converter ate (table-wrapped thumbnails are the common offender).
    for src in _IMG_SRC_RE.findall(raw_html):
        src = html.unescape(src)
        if src and src not in seen:
            urls.append(src)
            seen.add(src)

    return content, urls


# ── Image upload utility ───────────────────────────────────────────────────

_DELTA_STORE_URL: str | None = None
_DELTA_API_KEY: str = ""


def _delta_url() -> str:
    global _DELTA_STORE_URL
    if _DELTA_STORE_URL is None:
        _DELTA_STORE_URL = os.environ.get("DELTA_STORE_URL", "http://localhost:4246").rstrip("/")
    return _DELTA_STORE_URL


def _delta_headers() -> dict[str, str]:
    global _DELTA_API_KEY
    if not _DELTA_API_KEY:
        _DELTA_API_KEY = os.environ.get("DELTA_API_KEY", "")
    return {"X-API-Key": _DELTA_API_KEY} if _DELTA_API_KEY else {}


async def upload_image(
    image_url: str,
    http_client: httpx.AsyncClient | None = None,
) -> str | None:
    """Download an image by URL and store it as a content-addressable
    blob in delta-store. Returns the media_hash on success, None on
    failure.

    NO delta is written here. The caller attaches the returned hash to
    its own ProducedDelta via the media_hash field, so the rich text
    content and the image share one record. (Side-effect image deltas
    used to split each feed item into two records and strip the image
    from the rich one — that's the bug this avoids.)

    Pass http_client to reuse an existing session (preserves cookies,
    referer, etc. — needed for sites like Reddit that block hotlinking).
    """
    try:
        dl_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        if http_client:
            r = await http_client.get(image_url, headers=dl_headers)
        else:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                r = await client.get(image_url, headers=dl_headers)
        r.raise_for_status()
        image_bytes = r.content

        if not image_bytes:
            return None

        url = _delta_url() + "/media/upload"
        headers = _delta_headers()
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                files={"file": ("image", image_bytes, "application/octet-stream")},
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()
            return body.get("media_hash")
    except httpx.HTTPStatusError as e:
        log.debug("Image download failed (%s): %s", e.response.status_code, image_url)
        return None
    except Exception:
        log.warning("Image upload failed: %s", image_url)
        return None


async def extract_images(
    image_urls: list[str],
    http_client: httpx.AsyncClient | None = None,
) -> str | None:
    """Upload the first available image from a URL list. Returns
    media_hash on success, None if all downloads fail.

    Single-hash result by design — the current schema stores one
    media_hash per delta. Multi-image-per-delta is a future schema
    change; for now, RSS / Mastodon / etc. attach the cover image.
    """
    for url in image_urls:
        media_hash = await upload_image(url, http_client=http_client)
        if media_hash:
            return media_hash
    return None


@dataclass
class RawItem:
    """A single item yielded by a source's poll cycle."""

    id: str
    content: str
    timestamp: str | None = None
    title: str = ""
    url: str | None = None
    image_urls: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProducedDelta:
    """Ready-to-write delta, output of digest step."""

    content: str
    tags: list[str]
    source: str
    modality: str = "text"
    timestamp: str | None = None
    expires_at: str | None = None
    image_urls: list[str] = field(default_factory=list)
    media_hash: str | None = None


class SourceProducer(ABC):
    """Abstract interface for a source plugin.

    Concrete producers implement poll() and optionally override digest().
    The SourceRunner handles deduplication, LLM wrapping, and delta writes.
    """

    source_type: str = ""
    display_name: str = ""
    description: str = ""
    version: str = "0.1.0"
    author: str = "fathom"
    auth_type: str = "none"
    schedule_type: str = "poll"
    default_interval: str = "30m"
    digestion: str = "raw"
    default_expiry_days: float | None = 30
    expiry_configurable: bool = True

    @abstractmethod
    async def poll(self, config: dict, since: float | None = None) -> list[RawItem]:
        """Fetch items from the source. Return all items; dedup handled by runner."""
        ...

    def digest(self, item: RawItem, config: dict | None = None) -> ProducedDelta:
        """Convert a RawItem to a ProducedDelta.

        For digestion="llm" sources (opt-in), the runner compresses content
        through an LLM after this method runs. Default is "raw" — structure
        your output programmatically in this method.
        """
        return ProducedDelta(
            content=item.content,
            tags=self.default_tags(config or {}),
            source=self.source_type,
            timestamp=item.timestamp,
        )

    def default_tags(self, config: dict) -> list[str]:
        return [self.source_type]

    def validate_config(self, config: dict) -> list[str]:
        """Return error messages. Empty list = valid."""
        return []
