"""In-memory puddle — the Grand Loop's working substrate.

The loop deliberates in a small, fast, ephemeral store separate from the
durable lake. The puddle is consciousness/now — observes but doesn't
own. It holds voice thoughts, queued intents, recall hits, telepathy
mirrors of recent lake activity, and the witness's currently-alive
cards. Everything is TTL'd; nothing survives a process restart on its
own. Authored content (engaged cards, user seeds, witness output that
auto-authors on resonance) lives in the lake; telepathy refills the
puddle's anchors (crystal, mood) on every boot.

This module implements the puddle as a Python list of delta dicts,
guarded by an asyncio.Lock for writes. Reads are lock-free over a snapshot.
The query API mirrors the delta-store HTTP API surface the loop already
uses (tag filters, time windows, expiry semantics) so the loop's
controller code can talk to a Puddle instance the same way it used to
talk to httpx.AsyncClient(DELTA_STORE).

Semantic search is intentionally NOT implemented here. The loop's
settle metric and resonance lookups need embeddings; v1 either falls back
to tag-recency queries or routes those calls to the real lake's search
endpoint. We can add a local embedding cache later if it matters.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # delta-store emits trailing Z; Python's fromisoformat handles that
        # natively from 3.11 onward.
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class Puddle:
    """Single shared in-memory delta store.

    One instance per api process. The Grand Loop's worker, telepathy,
    and the /v1/puddle/* HTTP routes all hold a reference to the same
    Puddle. No per-contact partitioning — the loop is global by design
    (the user explicitly decided this; see the discussion that led to
    the open-forum cut).
    """

    def __init__(self) -> None:
        self._deltas: list[dict] = []
        self._lock = asyncio.Lock()
        # A single broadcast queue per subscriber. New writes fan out to
        # every live subscriber; slow consumers drop their oldest items
        # (subscribe-side bounded queue), so a stalled SSE client never
        # backpressures the writer.
        self._subscribers: set[asyncio.Queue] = set()

    # ── Writes ─────────────────────────────────────────────────────────

    async def write(
        self,
        *,
        content: str,
        tags: list[str],
        source: str,
        ttl_seconds: int | None = None,
        expires_at: str | None = None,
        embedding: list[float] | None = None,
        timestamp: str | None = None,
    ) -> dict:
        """Append a delta. Returns the stored dict (with id, timestamp).

        Pass exactly one of `ttl_seconds` or `expires_at`. Omit both for
        a delta that lives until process restart — used sparingly; most
        puddle writes should expire.

        ``embedding``: optional pre-computed embedding to attach. When
        set, resonance ranking uses this vector verbatim instead of
        embedding the content text. Used by recall-summary writes whose
        narrative content is the rendered timeline prose, but whose
        semantic anchor is the average of the lake passages that
        actually resonated — late-chunking shape: content carries the
        meaning, embedding carries the context.

        ``timestamp``: optional ISO timestamp override. Default is `now`
        (write time). Telepathy mirrors set this to the lake delta's
        original timestamp so the puddle entry sorts chronologically
        with the activity it represents — without this, every mirror
        stamps as "now" and the dashboard feed shows a burst of items
        all at the same moment after telepathy runs.
        """
        now = _now()
        if ttl_seconds is not None and expires_at is None:
            expires_at = _iso(now + timedelta(seconds=ttl_seconds))
        ts = timestamp or _iso(now)
        delta = {
            "id": str(uuid.uuid4()),
            "content": content,
            "tags": list(tags),
            "source": source,
            "timestamp": ts,
            "expires_at": expires_at,
        }
        if embedding is not None:
            delta["_embedding"] = list(embedding)
        async with self._lock:
            self._deltas.append(delta)
        # Fan out to subscribers outside the lock so one slow consumer
        # doesn't block the writer.
        for q in list(self._subscribers):
            try:
                q.put_nowait(delta)
            except asyncio.QueueFull:
                # Drop oldest; subscribe() builds bounded queues so a
                # stalled consumer self-heals rather than wedging the loop.
                try:
                    q.get_nowait()
                    q.put_nowait(delta)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return delta

    # ── Reads ──────────────────────────────────────────────────────────

    def query(
        self,
        *,
        tags_include: list[str] | None = None,
        tags_exclude: list[str] | None = None,
        limit: int = 200,
        time_start: str | None = None,
        time_end: str | None = None,
        order: str = "desc",
    ) -> list[dict]:
        """Filter alive deltas. Mirrors delta-store's `/deltas` query shape.

        `tags_include` is conjunctive (all must be present), matching
        delta-store. `tags_exclude` rejects any delta with any listed tag.
        Returns newest-first by default.
        """
        include = set(tags_include or [])
        exclude = set(tags_exclude or [])
        start = _parse_iso(time_start) if time_start else None
        end = _parse_iso(time_end) if time_end else None
        now = _now()

        out: list[dict] = []
        # Iterate over a snapshot so reads don't fight writes for the lock.
        snapshot = list(self._deltas)
        for d in snapshot:
            exp = _parse_iso(d.get("expires_at") or "")
            if exp is not None and exp <= now:
                continue
            tags = set(d.get("tags") or [])
            if include and not include.issubset(tags):
                continue
            if exclude and tags & exclude:
                continue
            if start or end:
                ts = _parse_iso(d.get("timestamp") or "")
                if ts is None:
                    continue
                if start and ts < start:
                    continue
                if end and ts > end:
                    continue
            out.append(d)

        out.sort(key=lambda d: d.get("timestamp") or "", reverse=(order == "desc"))
        return out[:limit]

    def get(self, delta_id: str) -> dict | None:
        """Look up a single delta by id. Returns None if missing or expired."""
        now = _now()
        for d in self._deltas:
            if d.get("id") != delta_id:
                continue
            exp = _parse_iso(d.get("expires_at") or "")
            if exp is not None and exp <= now:
                return None
            return d
        return None

    # ── Maintenance ────────────────────────────────────────────────────

    async def reap(self) -> int:
        """Drop expired deltas. Returns the number reaped.

        Queries already filter by expires_at, so unreaped corpses don't
        leak into results — reap is purely a memory-pressure measure.
        Call periodically (worker.py spawns a 30s reap task).
        """
        now = _now()
        async with self._lock:
            before = len(self._deltas)
            self._deltas = [
                d for d in self._deltas
                if (
                    (exp := _parse_iso(d.get("expires_at") or ""))
                    is None
                    or exp > now
                )
            ]
            return before - len(self._deltas)

    def stats(self) -> dict:
        """Quick health snapshot for /v1/puddle/stats and diagnostics."""
        now = _now()
        alive = 0
        expired_unreaped = 0
        for d in self._deltas:
            exp = _parse_iso(d.get("expires_at") or "")
            if exp is None or exp > now:
                alive += 1
            else:
                expired_unreaped += 1
        return {
            "alive": alive,
            "expired_unreaped": expired_unreaped,
            "subscribers": len(self._subscribers),
        }

    # ── Subscriptions (SSE / live viz feed) ────────────────────────────

    async def subscribe(self, *, maxsize: int = 256) -> AsyncIterator[dict]:
        """Async generator yielding new writes as they happen.

        Bounded per-subscriber queue: a slow consumer drops its oldest
        items rather than blocking the writer. Cancellation cleans up.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(q)
        try:
            while True:
                delta = await q.get()
                yield delta
        finally:
            self._subscribers.discard(q)


# Module-level singleton. Imported as `from .puddle import puddle`.
# The api process holds one Puddle for the whole instance; everything
# (worker, routes, viz) shares it. No per-contact scoping by design.
puddle = Puddle()
