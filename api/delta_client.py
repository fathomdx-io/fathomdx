"""Async HTTP client for the delta store API."""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

from .settings import settings

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

# Retry tuning for idempotent reads — transient 5xx and network hiccups
# between api and delta-store in a compose stack are not uncommon during
# delta-store restarts or postgres recovery pauses. Three attempts with
# jittered exponential backoff cover the usual case without turning a
# real outage into a hang. Writes DO NOT retry: delta-store has no
# idempotency keys and retrying a POST can create duplicate deltas.
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.2  # seconds
_RETRY_STATUS_CODES = frozenset({502, 503, 504})


async def _get() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        headers = {}
        if settings.delta_api_key:
            headers["X-API-Key"] = settings.delta_api_key
        _client = httpx.AsyncClient(
            base_url=settings.delta_store_url,
            headers=headers,
            timeout=30.0,
        )
    return _client


async def close():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


async def _request_with_retry(
    method: str,
    url: str,
    *,
    attempts: int = _RETRY_ATTEMPTS,
    **kwargs,
) -> httpx.Response:
    """Issue a request, retrying on transient network / 5xx failures.

    Only safe for idempotent requests — see module docstring on retries.
    Returns the Response on success; raises the last error on exhaustion.
    """
    c = await _get()
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            r = await c.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
        else:
            if r.status_code not in _RETRY_STATUS_CODES:
                return r
            last_exc = httpx.HTTPStatusError(
                f"server responded {r.status_code}", request=r.request, response=r
            )
        if attempt + 1 < attempts:
            # Jittered exponential backoff — the jitter avoids a
            # stampeding-herd of retries all landing at the same tick.
            delay = _RETRY_BASE_DELAY * (2**attempt) * (0.5 + random.random())
            log.warning(
                "delta-store %s %s failed (%s), retrying in %.2fs (attempt %d/%d)",
                method,
                url,
                last_exc,
                delay,
                attempt + 1,
                attempts,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


# ── Search ──────────────────────────────────────


async def search(
    query: str,
    limit: int = 20,
    radii: dict | None = None,
    tags_include: list[str] | None = None,
    include_engagement_cloud: bool = False,
) -> dict:
    body: dict = {"origin": query, "limit": min(limit, 50)}
    if radii:
        body["radii"] = radii
    if tags_include:
        body["tags_include"] = tags_include
    if include_engagement_cloud:
        body["include_engagement_cloud"] = True
    r = await _request_with_retry("POST", "/search", json=body)
    r.raise_for_status()
    return r.json()


# ── Embed ───────────────────────────────────────


async def embed(texts: list[str]) -> list[list[float]]:
    """Embed one or more texts via the lake's CLIP encoder.

    Returns a list of float vectors, one per input string. Used by the
    Grand Loop's resonance ranking. Empty input returns []. On any
    transport failure, the caller is responsible for falling back —
    resonance ranking should degrade to recency, not crash the loop.
    """
    if not texts:
        return []
    r = await _request_with_retry("POST", "/embed", json={"texts": texts})
    r.raise_for_status()
    return r.json().get("embeddings") or []


# ── Write ───────────────────────────────────────


async def write(
    content: str,
    tags: list[str] | None = None,
    source: str = "consumer-api",
    expires_at: str | None = None,
    media_hash: str | None = None,
) -> dict:
    c = await _get()
    body = {"content": content, "source": source, "tags": tags or []}
    if expires_at:
        body["expires_at"] = expires_at
    if media_hash:
        # Re-references existing media bytes by hash without re-uploading.
        # Used by engagement-snapshot to keep an affirmed image viewable
        # after the source delta reaps.
        body["media_hash"] = media_hash
    r = await c.post("/deltas", json=body)
    r.raise_for_status()
    return r.json()


# ── Query (structured filter) ───────────────────


async def query(
    limit: int = 50,
    tags_include: list[str] | None = None,
    tags_exclude: list[str] | None = None,
    source: str | None = None,
    time_start: str | None = None,
    time_end: str | None = None,
) -> list:
    params: dict = {"limit": limit}
    if tags_include:
        params["tags_include"] = tags_include
    if tags_exclude:
        params["tags_exclude"] = tags_exclude
    if source:
        params["source"] = source
    if time_start:
        params["time_start"] = time_start
    if time_end:
        params["time_end"] = time_end
    r = await _request_with_retry("GET", "/deltas", params=params)
    r.raise_for_status()
    return r.json()


# ── Plan (compositional query) ──────────────────


async def plan(steps: list[dict]) -> dict:
    r = await _request_with_retry("POST", "/plan", json={"steps": steps})
    r.raise_for_status()
    return r.json()


# ── Engagement cloud ────────────────────────────


async def engagement_cloud(delta_ids: list[str]) -> dict:
    """Batched lookup: for each delta id, return deltas pointing at it via any
    engagement pointer-tag. Returns {"<id>": [DeltaSlim, ...], ...}."""
    if not delta_ids:
        return {}
    r = await _request_with_retry("POST", "/engagement-cloud", json={"delta_ids": delta_ids})
    r.raise_for_status()
    return r.json()


# ── Single delta ────────────────────────────────


async def get_delta(delta_id: str) -> dict:
    r = await _request_with_retry("GET", f"/deltas/{delta_id}")
    r.raise_for_status()
    return r.json()


async def batch_get(ids: list[str]) -> list[dict]:
    """Bulk fetch by id. Order is not guaranteed; missing ids are
    silently dropped (the lake is the source of truth, not the caller's
    list). Capped server-side at 500 per call."""
    if not ids:
        return []
    r = await _request_with_retry("POST", "/deltas/batch-get", json={"ids": ids})
    r.raise_for_status()
    return r.json()


# ── Meta ────────────────────────────────────────


async def tags() -> dict:
    r = await _request_with_retry("GET", "/tags")
    r.raise_for_status()
    return r.json()


async def stats() -> dict:
    r = await _request_with_retry("GET", "/stats")
    r.raise_for_status()
    return r.json()


async def retrievals_history(since_seconds: int, buckets: int = 60) -> list[dict]:
    """Fetch bucketed delta-retrieval timeline from the lake."""
    r = await _request_with_retry(
        "GET",
        "/stats/retrievals/history",
        params={"since_seconds": since_seconds, "buckets": buckets},
    )
    r.raise_for_status()
    return r.json().get("history", [])


async def usage_history(since_seconds: int, buckets: int = 60) -> list[dict]:
    """Fetch bucketed delta-write timeline from the lake (SQL-bucketed, no row cap)."""
    r = await _request_with_retry(
        "GET",
        "/stats/usage/history",
        params={"since_seconds": since_seconds, "buckets": buckets},
    )
    r.raise_for_status()
    return r.json().get("history", [])


async def pressure_history(
    *,
    since_seconds: int,
    buckets: int,
    weights: dict[str, float],
    default_weight: float,
    user_tag_boost: float,
    half_life_seconds: int,
) -> list[dict]:
    """Fetch bucketed weighted-decay pressure curve (SQL-computed, no row cap)."""
    r = await _request_with_retry(
        "POST",
        "/stats/pressure/history",
        json={
            "since_seconds": since_seconds,
            "buckets": buckets,
            "weights": weights,
            "default_weight": default_weight,
            "user_tag_boost": user_tag_boost,
            "half_life_seconds": half_life_seconds,
        },
    )
    r.raise_for_status()
    return r.json().get("history", [])


async def pressure_volume(
    *,
    cutoff_ts: str | None,
    window_seconds: int,
    weights: dict[str, float],
    default_weight: float,
    user_tag_boost: float,
    half_life_seconds: int,
) -> float:
    """Single weighted-decay pressure value since cutoff (or window)."""
    r = await _request_with_retry(
        "POST",
        "/stats/pressure/volume",
        json={
            "cutoff_ts": cutoff_ts,
            "window_seconds": window_seconds,
            "weights": weights,
            "default_weight": default_weight,
            "user_tag_boost": user_tag_boost,
            "half_life_seconds": half_life_seconds,
        },
    )
    r.raise_for_status()
    return float(r.json().get("volume", 0.0))


async def upload_media(
    file_bytes: bytes,
    filename: str,
    content: str = "",
    tags: list[str] | None = None,
    source: str = "fathom-chat",
    expires_at: str | None = None,
) -> dict:
    """Upload an image to the delta store, returns {id, media_hash}."""
    import io

    c = await _get()
    files = {"file": (filename, io.BytesIO(file_bytes), "application/octet-stream")}
    data: dict = {
        "content": content,
        "tags": ",".join(tags or []),
        "source": source,
    }
    if expires_at:
        data["expires_at"] = expires_at
    r = await c.post("/deltas/media/upload", files=files, data=data, timeout=30)
    r.raise_for_status()
    return r.json()


async def recent_deltas_timestamps(limit: int = 5000) -> list[str]:
    """Fetch timestamps of recent deltas for the usage chart."""
    r = await _request_with_retry("GET", "/deltas", params={"limit": limit})
    r.raise_for_status()
    return [d.get("timestamp", "")[:10] for d in r.json() if d.get("timestamp")]


async def feed_stories(
    limit: int = 50,
    offset: int = 0,
    contact_slug: str | None = None,
) -> dict:
    params: dict = {"limit": limit, "offset": offset}
    # delta-store's /feed/stories accepts an optional `layer` tag filter
    # to narrow by a second tag. Repurpose it as the contact scope so each
    # dashboard only sees its own contact's cards.
    if contact_slug:
        params["layer"] = f"contact:{contact_slug}"
    r = await _request_with_retry("GET", "/feed/stories", params=params)
    r.raise_for_status()
    return r.json()


async def drift(text: str, since: str | None = None) -> dict:
    """Compute crystal drift via the delta-store's /drift endpoint.

    Returns {drift, new_deltas, total_deltas}. Drift is cosine distance
    (0 = aligned, ~2 = opposite) between the supplied text's embedding
    and the lake's exponentially-decayed centroid (7-day half-life).

    Used at crystal-write time to validate that a candidate crystal
    actually describes the lake — drift outside the accept band (see
    api.server.refresh_crystal) means the LLM produced an artifact that
    doesn't reflect current mental state.
    """
    body = {"text": text, "since": since or ""}
    r = await _request_with_retry("POST", "/drift", json=body, timeout=20)
    r.raise_for_status()
    return r.json()


# ── Contacts registry (minimal: slug + created_at + disabled_at) ─────────


async def get_contact_row(slug: str, include_disabled: bool = False) -> dict | None:
    """Fetch the thin registry row. The full contact dict lives in the
    profile delta; consumer-api's contacts module merges the two."""
    params = {"include_disabled": "true"} if include_disabled else {}
    r = await _request_with_retry("GET", f"/contacts/{slug}", params=params)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


async def list_contact_rows(include_disabled: bool = False) -> list[dict]:
    r = await _request_with_retry("GET", "/contacts")
    r.raise_for_status()
    rows = r.json()
    if include_disabled:
        return rows
    return [row for row in rows if not row.get("disabled_at")]


async def create_contact_row(slug: str) -> dict:
    c = await _get()
    r = await c.post("/contacts", json={"slug": slug})
    r.raise_for_status()
    return r.json()


async def disable_contact_row(slug: str) -> dict:
    c = await _get()
    r = await c.post(f"/contacts/{slug}/disable")
    r.raise_for_status()
    return r.json()


async def reenable_contact_row(slug: str) -> dict:
    c = await _get()
    r = await c.post(f"/contacts/{slug}/reenable")
    r.raise_for_status()
    return r.json()


async def list_handles(slug: str) -> list[dict]:
    r = await _request_with_retry("GET", f"/contacts/{slug}/handles")
    r.raise_for_status()
    return r.json()


async def add_handle(slug: str, channel: str, identifier: str) -> dict:
    c = await _get()
    r = await c.post(
        f"/contacts/{slug}/handles",
        json={"channel": channel, "identifier": identifier},
    )
    r.raise_for_status()
    return r.json()


async def remove_handle(slug: str, channel: str, identifier: str) -> None:
    c = await _get()
    r = await c.request(
        "DELETE",
        f"/contacts/{slug}/handles",
        json={"channel": channel, "identifier": identifier},
    )
    r.raise_for_status()


async def resolve_handle(channel: str, identifier: str) -> str | None:
    r = await _request_with_retry(
        "GET",
        "/handles/resolve",
        params={"channel": channel, "identifier": identifier},
    )
    r.raise_for_status()
    return r.json().get("contact_slug")


async def backfill_contact_tag(contact_slug: str, filter_tags: list[str]) -> dict:
    """Append contact:<slug> to legacy per-user deltas that predate the
    contact registry. Idempotent — safe to call every boot."""
    c = await _get()
    r = await c.post(
        "/admin/backfill-contact-tag",
        json={"contact_slug": contact_slug, "filter_tags": filter_tags},
    )
    r.raise_for_status()
    return r.json()


async def centroid(tags_include: list[str] | None = None) -> dict:
    """Fetch the raw lake centroid vector from delta-store.

    Returns {centroid: [floats]|None, dim, total_deltas}. Called at
    crystal-write time to snapshot the anchor, and at each drift tick
    to compute how far the lake has moved since the anchor was set.

    `tags_include` scopes the centroid to a tagged subset (used by the
    feed-orient crystal to anchor on `feed-engagement` deltas).
    """
    params = {}
    if tags_include:
        params["tags_include"] = ",".join(tags_include)
    r = await _request_with_retry("GET", "/centroid", params=params, timeout=20)
    r.raise_for_status()
    return r.json()
