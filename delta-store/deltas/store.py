"""Async Postgres-backed delta store.

Pure storage layer: write, read, update, delete. No HTTP, no embeddings,
no query logic beyond basic filtering. All embedding distance computation
happens in SQL via pgvector.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
import numpy as np


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO timestamp string to a timezone-aware datetime."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts).replace(tzinfo=UTC)


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
        rows = await self._pool.fetch(
            "SELECT * FROM deltas WHERE embedding IS NULL ORDER BY timestamp DESC LIMIT $1",
            limit,
        )
        return [_row_to_delta(r) for r in rows]

    async def update_embeddings(
        self, delta_id: str, embedding: list[float], provenance_embedding: list[float]
    ) -> None:
        emb = np.array(embedding, dtype=np.float32)
        prov = np.array(provenance_embedding, dtype=np.float32)
        await self._pool.execute(
            "UPDATE deltas SET embedding = $1, provenance_embedding = $2 WHERE id = $3",
            emb,
            prov,
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

    async def reap_expired(self) -> int:
        """Delete deltas whose expires_at is in the past. Returns count deleted."""
        result = await self._pool.execute(
            "DELETE FROM deltas WHERE expires_at IS NOT NULL AND expires_at <= NOW()"
        )
        # result is like "DELETE 5"
        return int(result.split()[-1])
