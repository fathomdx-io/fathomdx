"""Async Postgres-backed delta store.

Pure storage layer: write, read, update, delete. No HTTP, no embeddings,
no query logic beyond basic filtering. All embedding distance computation
happens in SQL via pgvector.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import numpy as np


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


_REL_TIME_RE = re.compile(
    r"^\s*(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks)\s*ago\s*$",
    re.IGNORECASE,
)
_REL_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "wk": 604800,
    "wks": 604800,
    "week": 604800,
    "weeks": 604800,
}


def _parse_ts(ts: str) -> datetime:
    """Parse a timestamp string to a timezone-aware datetime.

    Accepts:
      · ISO 8601 (`2026-05-07T18:00:00Z`, `2026-05-07T18:00:00+00:00`)
      · natural-language relative ("6 hours ago", "10 minutes ago",
        "1 day ago"). Helpers / agents often emit these from instruction
        text without round-tripping through a date library; treat them
        as first-class to avoid 500s on the read path.

    Anything else raises ValueError (caller surfaces 400, not 500).
    """
    ts = (ts or "").strip()
    rel = _REL_TIME_RE.match(ts)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        seconds = n * _REL_UNIT_SECONDS[unit]
        return datetime.now(UTC) - timedelta(seconds=seconds)
    iso = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


def _format_ts(dt: datetime) -> str:
    """Format a datetime to ISO string with Z suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _vec_to_list(v) -> list[float]:
    """Convert a pgvector numpy array or None to a Python list."""
    if v is None:
        return []
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, list):
        return v
    if hasattr(v, "to_list"):        # pgvector >= 0.4 returns a Vector object
        return v.to_list()
    if hasattr(v, "to_numpy"):
        return v.to_numpy().tolist()
    return list(v)


def _row_to_delta(row: asyncpg.Record) -> dict:
    """Convert a database row to a delta dict matching v1 shape."""
    d = {
        "id": row["id"],
        "timestamp": _format_ts(row["timestamp"]),
        "modality": row["modality"],
        "content": row["content"],
        "embedding": _vec_to_list(row["embedding"]),
        "provenance_embedding": _vec_to_list(row["provenance_embedding"]),
        "source": row["source"],
        "tags": list(row["tags"]) if row["tags"] else [],
    }
    # image_embedding is nullable and optional — populated for deltas
    # whose media_hash had a CLIP-image vector computed at embed time.
    img_emb = row["image_embedding"] if "image_embedding" in row else None
    if img_emb is not None:
        d["image_embedding"] = _vec_to_list(img_emb)
    if row["media_hash"]:
        d["media_hash"] = row["media_hash"]
    if row["expires_at"]:
        d["expires_at"] = _format_ts(row["expires_at"])
    return d


class DeltaStore:
    """Async Postgres delta store — pure data access."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    # ── Write ────────────────────────────────────────────────────────────

    async def write(
        self,
        *,
        content: str,
        modality: str = "text",
        tags: list[str] | None = None,
        timestamp: str | None = None,
        id: str | None = None,
        embedding: list[float] | None = None,
        provenance_embedding: list[float] | None = None,
        source: str = "unknown",
        media_hash: str | None = None,
        expires_at: str | None = None,
    ) -> str | None:
        """Write a single delta. Returns the delta id, or None if deduped.

        Sequential dedup: if the most recent delta with the same source + tags
        has identical content, the write is skipped. This prevents repeated
        writes of unchanged data (e.g. vault files, sensor readings) while
        allowing the same value to reappear after a different value.
        """
        delta_id = id or new_id()
        ts = _parse_ts(timestamp) if timestamp else datetime.now(UTC)
        tags = tags or []
        exp = _parse_ts(expires_at) if expires_at else None

        # Sequential dedup: check the most recent delta with same source + tags
        # Skip for media writes — each image upload is an explicit observation
        # ("user sent this now"), and the chat UI expects one delta per send
        # even when the same bytes are re-sent. Media files are already
        # content-dedup'd by hash at the blob layer.
        if tags and source and media_hash is None:
            prev = await self._pool.fetchrow(
                """
                SELECT content FROM deltas
                WHERE source = $1 AND tags @> $2
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                source,
                tags,
            )
            if prev and prev["content"] == content:
                return None  # Not a delta — identical to the previous value

        # Convert embeddings to numpy for pgvector
        emb = np.array(embedding, dtype=np.float32) if embedding else None
        prov_emb = (
            np.array(provenance_embedding, dtype=np.float32) if provenance_embedding else None
        )

        await self._pool.execute(
            """
            INSERT INTO deltas (id, timestamp, modality, content, embedding,
                                provenance_embedding, source, tags, media_hash, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (id) DO UPDATE SET
                timestamp = EXCLUDED.timestamp,
                modality = EXCLUDED.modality,
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                provenance_embedding = EXCLUDED.provenance_embedding,
                source = EXCLUDED.source,
                tags = EXCLUDED.tags,
                media_hash = EXCLUDED.media_hash,
                expires_at = EXCLUDED.expires_at
            """,
            delta_id,
            ts,
            modality,
            content,
            emb,
            prov_emb,
            source,
            tags,
            media_hash,
            exp,
        )
        return delta_id

    async def write_batch(self, deltas: list[dict]) -> int:
        """Write multiple deltas in one transaction. Returns count written."""
        count = 0
        async with self._pool.acquire() as conn, conn.transaction():
            for d in deltas:
                delta_id = d.get("id") or new_id()
                ts_str = d.get("timestamp")
                ts = _parse_ts(ts_str) if ts_str else datetime.now(UTC)
                tags = d.get("tags", [])
                exp_str = d.get("expires_at")
                exp = _parse_ts(exp_str) if exp_str else None
                emb = np.array(d["embedding"], dtype=np.float32) if d.get("embedding") else None
                prov = (
                    np.array(d["provenance_embedding"], dtype=np.float32)
                    if d.get("provenance_embedding")
                    else None
                )

                await conn.execute(
                    """
                        INSERT INTO deltas (id, timestamp, modality, content, embedding,
                                            provenance_embedding, source, tags, media_hash,
                                            expires_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        ON CONFLICT (id) DO UPDATE SET
                            timestamp = EXCLUDED.timestamp,
                            modality = EXCLUDED.modality,
                            content = EXCLUDED.content,
                            embedding = EXCLUDED.embedding,
                            provenance_embedding = EXCLUDED.provenance_embedding,
                            source = EXCLUDED.source,
                            tags = EXCLUDED.tags,
                            media_hash = EXCLUDED.media_hash,
                            expires_at = EXCLUDED.expires_at
                        """,
                    delta_id,
                    ts,
                    d.get("modality", "text"),
                    d["content"],
                    emb,
                    prov,
                    d.get("source", "unknown"),
                    tags,
                    d.get("media_hash"),
                    exp,
                )
                count += 1
        return count

    # ── Read ─────────────────────────────────────────────────────────────

    async def get(self, delta_id: str) -> dict | None:
        """Get a single delta by id. Supports prefix matching for short IDs."""
        row = await self._pool.fetchrow("SELECT * FROM deltas WHERE id = $1", delta_id)
        if row is None and len(delta_id) >= 8:
            rows = await self._pool.fetch(
                "SELECT * FROM deltas WHERE id LIKE $1 LIMIT 2", delta_id + "%"
            )
            if len(rows) == 1:
                row = rows[0]
        if row is None:
            return None
        return _row_to_delta(row)

    async def query(
        self,
        *,
        time_start: str | None = None,
        time_end: str | None = None,
        tags_include: list[str] | None = None,
        tags_exclude: list[str] | None = None,
        modality: str | None = None,
        source: str | None = None,
        has_media: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Query deltas by temporal range, tags, and modality."""
        conditions: list[str] = ["(d.expires_at IS NULL OR d.expires_at > NOW())"]
        params: list = []
        idx = 1

        if time_start:
            conditions.append(f"d.timestamp >= ${idx}")
            params.append(_parse_ts(time_start))
            idx += 1
        if time_end:
            conditions.append(f"d.timestamp <= ${idx}")
            params.append(_parse_ts(time_end))
            idx += 1
        if modality:
            conditions.append(f"d.modality = ${idx}")
            params.append(modality)
            idx += 1
        if source:
            conditions.append(f"d.source = ${idx}")
            params.append(source)
            idx += 1
        if tags_include:
            conditions.append(f"d.tags @> ${idx}")
            params.append(tags_include)
            idx += 1
        if tags_exclude:
            conditions.append(f"NOT (d.tags && ${idx})")
            params.append(tags_exclude)
            idx += 1
        if has_media is True:
            conditions.append("d.media_hash IS NOT NULL")
        elif has_media is False:
            conditions.append("d.media_hash IS NULL")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"""
            SELECT * FROM deltas d {where}
            ORDER BY d.timestamp DESC
            LIMIT ${idx} OFFSET ${idx + 1}
        """
        params.extend([limit, offset])

        rows = await self._pool.fetch(sql, *params)
        return [_row_to_delta(r) for r in rows]

    async def count(self, *, modality: str | None = None, tag: str | None = None) -> int:
        if tag:
            row = await self._pool.fetchrow(
                "SELECT COUNT(*) AS c FROM deltas WHERE $1 = ANY(tags)", tag
            )
        elif modality:
            row = await self._pool.fetchrow(
                "SELECT COUNT(*) AS c FROM deltas WHERE modality = $1", modality
            )
        else:
            row = await self._pool.fetchrow("SELECT COUNT(*) AS c FROM deltas")
        return row["c"]

    async def sources(self) -> dict[str, int]:
        rows = await self._pool.fetch(
            "SELECT source, COUNT(*) AS c FROM deltas GROUP BY source ORDER BY c DESC"
        )
        return {r["source"]: r["c"] for r in rows}

    async def tags(self) -> dict[str, int]:
        rows = await self._pool.fetch(
            "SELECT t, COUNT(*) AS c FROM deltas, unnest(tags) AS t GROUP BY t ORDER BY c DESC"
        )
        return {r["t"]: r["c"] for r in rows}

    async def pressure_history(
        self,
        *,
        since_seconds: int,
        buckets: int,
        weights: dict[str, float],
        default_weight: float,
        user_tag_boost: float,
        half_life_seconds: int,
    ) -> list[tuple[int, float]]:
        """Bucketed weighted-decay pressure curve, computed entirely in SQL.

        For each of N buckets across the window, compute:
            pressure(tick) = Σ w(d) × 0.5^((tick − ts(d)) / half_life)
        summed over every delta d whose timestamp is after the most recent
        'mood-delta' synthesis at-or-before `tick` and at-or-before `tick`.

        Returns (bucket_index, pressure_value) pairs for every bucket.
        """
        if since_seconds <= 0 or buckets <= 0:
            return []
        import json as _json

        # Parameters go in directly as typed literals — wrapping them in a
        # params CTE cross-joined against deltas produces a pathological plan
        # (observed: minutes-long queries for a 60-bucket × 18k-delta window).
        rows = await self._pool.fetch(
            """
            WITH bucket_ticks AS (
                SELECT
                    i::int AS i,
                    (NOW() - ($1 || ' seconds')::interval)
                        + ((i + 0.5) * ($1::float / $2)) * INTERVAL '1 second' AS t
                FROM generate_series(0, $2 - 1) AS i
            ),
            synth_events AS (
                SELECT timestamp AS ts
                FROM deltas
                WHERE 'mood-delta' = ANY(tags)
                  AND timestamp >= NOW() - ($1 || ' seconds')::interval
                  AND (expires_at IS NULL OR expires_at > NOW())
            ),
            weighted_deltas AS (
                SELECT
                    timestamp AS ts,
                    (
                        COALESCE(($4::jsonb ->> source)::float, $5::float)
                        + CASE WHEN 'user' = ANY(tags) THEN $6::float ELSE 0 END
                    ) AS w
                FROM deltas
                WHERE timestamp >= NOW() - ($1 || ' seconds')::interval
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND NOT ('mood-delta' = ANY(tags))
            )
            SELECT
                bt.i AS bucket,
                COALESCE(
                    SUM(
                        wd.w * POWER(
                            0.5,
                            EXTRACT(EPOCH FROM (bt.t - wd.ts)) / $3::float
                        )
                    ),
                    0
                ) AS v
            FROM bucket_ticks bt
            LEFT JOIN weighted_deltas wd
                ON wd.w > 0
               AND wd.ts <= bt.t
               AND wd.ts > COALESCE(
                        (SELECT MAX(se.ts) FROM synth_events se WHERE se.ts <= bt.t),
                        '-infinity'::timestamptz
                    )
            GROUP BY bt.i
            ORDER BY bt.i
            """,
            str(since_seconds),
            buckets,
            float(max(1, int(half_life_seconds))),
            _json.dumps(weights),
            float(default_weight),
            float(user_tag_boost),
        )
        return [(int(r["bucket"]), float(r["v"])) for r in rows]

    async def pressure_volume(
        self,
        *,
        cutoff_ts: str | None,
        window_seconds: int,
        weights: dict[str, float],
        default_weight: float,
        user_tag_boost: float,
        half_life_seconds: int,
    ) -> float:
        """Sum of weighted-and-decayed contributions since cutoff.

        If cutoff_ts is None, uses NOW() − window_seconds as the cutoff.
        Returns a single pressure volume (the same number read_pressure wants).
        """
        import json as _json

        cutoff_dt = _parse_ts(cutoff_ts) if cutoff_ts else None
        row = await self._pool.fetchrow(
            """
            SELECT COALESCE(
                SUM(
                    (
                        COALESCE(($4::jsonb ->> source)::float, $5::float)
                        + CASE WHEN 'user' = ANY(tags) THEN $6::float ELSE 0 END
                    )
                    * POWER(
                        0.5,
                        EXTRACT(EPOCH FROM (NOW() - timestamp)) / $3::float
                    )
                ),
                0
            ) AS volume
            FROM deltas
            WHERE timestamp > COALESCE($1::timestamptz, NOW() - ($2 || ' seconds')::interval)
              AND (expires_at IS NULL OR expires_at > NOW())
              AND NOT ('mood-delta' = ANY(tags))
              AND (
                    COALESCE(($4::jsonb ->> source)::float, $5::float)
                    + CASE WHEN 'user' = ANY(tags) THEN $6::float ELSE 0 END
                  ) > 0
            """,
            cutoff_dt,
            str(window_seconds),
            float(max(1, int(half_life_seconds))),
            _json.dumps(weights),
            float(default_weight),
            float(user_tag_boost),
        )
        return float(row["volume"] or 0.0)

    async def usage_history(self, since_seconds: int, buckets: int = 60) -> list[tuple[int, int]]:
        """Bucketed write-count timeline over the window.

        Returns a list of (bucket_index, count) pairs for non-empty buckets.
        Bucketing is done in SQL so there's no row-limit truncation.
        """
        if since_seconds <= 0 or buckets <= 0:
            return []
        bucket_seconds = since_seconds / buckets
        rows = await self._pool.fetch(
            """
            SELECT
                LEAST(
                    FLOOR(EXTRACT(EPOCH FROM (d.timestamp - (NOW() - ($1 || ' seconds')::interval)))
                          / $2)::int,
                    $3 - 1
                ) AS bucket,
                COUNT(*) AS c
            FROM deltas d
            WHERE d.timestamp >= NOW() - ($1 || ' seconds')::interval
              AND (d.expires_at IS NULL OR d.expires_at > NOW())
            GROUP BY bucket
            ORDER BY bucket
            """,
            str(since_seconds),
            bucket_seconds,
            buckets,
        )
        return [(int(r["bucket"]), int(r["c"])) for r in rows]

    # ── Embeddings ───────────────────────────────────────────────────────

    async def unembedded(self, limit: int = 50) -> list[dict]:
        # Two cases qualify:
        #  · No text embedding yet — fresh writes, embed-loop computes
        #    text + image (if media) in one pass.
        #  · Has text embedding but missing image_embedding while
        #    carrying media — legacy backfill. Pre-split, image content
        #    was averaged into `embedding`; the new shape stores it
        #    separately. The loop recomputes both axes cleanly.
        rows = await self._pool.fetch(
            "SELECT * FROM deltas "
            "WHERE embedding IS NULL "
            "   OR (media_hash IS NOT NULL AND image_embedding IS NULL) "
            "ORDER BY timestamp DESC LIMIT $1",
            limit,
        )
        return [_row_to_delta(r) for r in rows]

    async def update_text_embedding_only(self, delta_id: str, embedding: list[float]) -> None:
        """Update only the content embedding column, leaving
        provenance_embedding untouched. Used by the embed loop for
        kind:provenance deltas whose provenance_embedding is a
        pre-computed centroid that the loop's tag-string default
        would otherwise clobber.
        """
        emb = np.array(embedding, dtype=np.float32)
        await self._pool.execute(
            "UPDATE deltas SET embedding = $1 WHERE id = $2",
            emb,
            delta_id,
        )

    async def update_provenance_embedding_only(
        self, delta_id: str, provenance_embedding: list[float]
    ) -> None:
        """Update only the provenance_embedding column, leaving the
        content embedding untouched. Used by the centroid backfill
        that retroactively populates centroids on existing
        kind:provenance deltas without disturbing the embed loop's
        content embeddings.
        """
        prov = np.array(provenance_embedding, dtype=np.float32)
        await self._pool.execute(
            "UPDATE deltas SET provenance_embedding = $1 WHERE id = $2",
            prov,
            delta_id,
        )

    async def update_embeddings(
        self,
        delta_id: str,
        embedding: list[float],
        provenance_embedding: list[float],
        image_embedding: list[float] | None = None,
    ) -> None:
        emb = np.array(embedding, dtype=np.float32) if embedding else None
        prov = np.array(provenance_embedding, dtype=np.float32) if provenance_embedding else None
        if image_embedding is None:
            await self._pool.execute(
                "UPDATE deltas SET embedding = $1, provenance_embedding = $2 WHERE id = $3",
                emb,
                prov,
                delta_id,
            )
            return
        img = np.array(image_embedding, dtype=np.float32) if image_embedding else None
        await self._pool.execute(
            "UPDATE deltas SET embedding = $1, provenance_embedding = $2, "
            "image_embedding = $3 WHERE id = $4",
            emb,
            prov,
            img,
            delta_id,
        )

    async def update_image_embedding_only(
        self, delta_id: str, image_embedding: list[float]
    ) -> None:
        """Backfill / set the CLIP-image vector without touching the
        text or provenance embeddings."""
        img = np.array(image_embedding, dtype=np.float32)
        await self._pool.execute(
            "UPDATE deltas SET image_embedding = $1 WHERE id = $2",
            img,
            delta_id,
        )

    async def embedding_stats(self) -> dict:
        row = await self._pool.fetchrow(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(embedding) AS embedded
            FROM deltas
            """
        )
        total = row["total"]
        embedded = row["embedded"]
        pending = total - embedded
        return {
            "total": total,
            "embedded": embedded,
            "pending": pending,
            "percent": round(embedded / total * 100, 1) if total > 0 else 0,
        }

    async def embedded_rows(self) -> list[asyncpg.Record]:
        """Return all deltas with embeddings (for strata PCA)."""
        return await self._pool.fetch(
            """
            SELECT id, timestamp, source, modality, LENGTH(content) AS content_length, embedding
            FROM deltas WHERE embedding IS NOT NULL ORDER BY timestamp
            """
        )

    # ── Export / Import ──────────────────────────────────────────────────

    async def export_iter(
        self,
        *,
        time_start: str | None = None,
        time_end: str | None = None,
        tags_include: list[str] | None = None,
        source: str | None = None,
    ) -> AsyncIterator[dict]:
        """Yield all matching deltas as dicts (no embeddings). For JSONL export."""
        conditions: list[str] = []
        params: list = []
        idx = 1

        if time_start:
            conditions.append(f"d.timestamp >= ${idx}")
            params.append(_parse_ts(time_start))
            idx += 1
        if time_end:
            conditions.append(f"d.timestamp <= ${idx}")
            params.append(_parse_ts(time_end))
            idx += 1
        if source:
            conditions.append(f"d.source = ${idx}")
            params.append(source)
            idx += 1
        if tags_include:
            conditions.append(f"d.tags @> ${idx}")
            params.append(tags_include)
            idx += 1

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"""
            SELECT id, timestamp, modality, content, source, tags, media_hash, expires_at
            FROM deltas d {where} ORDER BY d.timestamp
        """

        async with self._pool.acquire() as conn:
            async for row in conn.cursor(sql, *params):
                d = {
                    "id": row["id"],
                    "timestamp": _format_ts(row["timestamp"]),
                    "modality": row["modality"],
                    "content": row["content"],
                    "source": row["source"],
                    "tags": list(row["tags"]) if row["tags"] else [],
                    "media_hash": row["media_hash"],
                }
                if row["expires_at"]:
                    d["expires_at"] = _format_ts(row["expires_at"])
                yield d

    async def import_batch(self, deltas: list[dict], *, skip_duplicates: bool = True) -> dict:
        """Import deltas (e.g. from JSONL). Returns {written, skipped, errors}."""
        stats = {"written": 0, "skipped": 0, "errors": 0}
        async with self._pool.acquire() as conn, conn.transaction():
            for d in deltas:
                delta_id = d.get("id") or new_id()
                if skip_duplicates:
                    exists = await conn.fetchval("SELECT 1 FROM deltas WHERE id = $1", delta_id)
                    if exists:
                        stats["skipped"] += 1
                        continue
                try:
                    ts_str = d.get("timestamp")
                    ts = _parse_ts(ts_str) if ts_str else datetime.now(UTC)
                    exp_str = d.get("expires_at")
                    exp = _parse_ts(exp_str) if exp_str else None
                    tags = d.get("tags", [])

                    await conn.execute(
                        """
                            INSERT INTO deltas (id, timestamp, modality, content,
                                                embedding, provenance_embedding,
                                                source, tags, media_hash, expires_at)
                            VALUES ($1, $2, $3, $4, NULL, NULL, $5, $6, $7, $8)
                            ON CONFLICT (id) DO UPDATE SET
                                timestamp = EXCLUDED.timestamp,
                                modality = EXCLUDED.modality,
                                content = EXCLUDED.content,
                                source = EXCLUDED.source,
                                tags = EXCLUDED.tags,
                                media_hash = EXCLUDED.media_hash,
                                expires_at = EXCLUDED.expires_at
                            """,
                        delta_id,
                        ts,
                        d.get("modality", "text"),
                        d["content"],
                        d.get("source", "unknown"),
                        tags,
                        d.get("media_hash"),
                        exp,
                    )
                    stats["written"] += 1
                except Exception:
                    stats["errors"] += 1
        return stats

    # ── Delete ───────────────────────────────────────────────────────────

    async def delete(self, delta_id: str) -> bool:
        result = await self._pool.execute("DELETE FROM deltas WHERE id = $1", delta_id)
        return result == "DELETE 1"

    async def reap_expired(self) -> tuple[int, list[str]]:
        """Delete deltas whose expires_at is in the past.

        Returns (deleted_count, orphaned_media_hashes). A media_hash is
        orphaned when none of the surviving deltas still reference it —
        media is content-addressable so a hash shared by two deltas
        survives until both reap. Caller is expected to delete the
        orphan files (this layer doesn't know about MEDIA_DIR).
        """
        async with self._pool.acquire() as conn, conn.transaction():
            expired_hashes = [
                r["media_hash"]
                for r in await conn.fetch(
                    "SELECT DISTINCT media_hash FROM deltas "
                    "WHERE expires_at IS NOT NULL "
                    "  AND expires_at <= NOW() "
                    "  AND media_hash IS NOT NULL"
                )
            ]
            result = await conn.execute(
                "DELETE FROM deltas WHERE expires_at IS NOT NULL AND expires_at <= NOW()"
            )
            deleted = int(result.split()[-1]) if result.startswith("DELETE ") else 0

            orphans: list[str] = []
            for h in expired_hashes:
                still_referenced = await conn.fetchval(
                    "SELECT 1 FROM deltas WHERE media_hash = $1 LIMIT 1", h
                )
                if not still_referenced:
                    orphans.append(h)
            return deleted, orphans
