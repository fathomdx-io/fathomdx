"""One-shot migration: SQLite delta store → Postgres + pgvector.

Usage:
    python -m deltas.migrate --sqlite /data/deltas.db --pg postgresql://fathom:fathom@localhost:5432/deltas
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sqlite3
import struct
import sys
from datetime import UTC, datetime

import asyncpg
import numpy as np
from pgvector.asyncpg import register_vector


def _unpack_blob(blob: bytes | None) -> np.ndarray | None:
    """Unpack a v1 float32 BLOB embedding to numpy array."""
    if not blob:
        return None
    count = len(blob) // 4
    floats = struct.unpack(f"{count}f", blob)
    return np.array(floats, dtype=np.float32)


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO timestamp to timezone-aware datetime."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts).replace(tzinfo=UTC)


def _fetch_all_tags(sqlite_conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Batch-fetch all tags from the junction table."""
    sqlite_conn.row_factory = sqlite3.Row
    rows = sqlite_conn.execute("SELECT delta_id, tag FROM delta_tags").fetchall()
    tags: dict[str, list[str]] = {}
    for r in rows:
        tags.setdefault(r["delta_id"], []).append(r["tag"])
    return tags


async def migrate(sqlite_path: str, pg_dsn: str, batch_size: int = 500) -> dict:
    """Run the migration. Returns stats dict."""
    # Open SQLite read-only
    sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sqlite_conn.row_factory = sqlite3.Row

    # Connect to Postgres
    conn = await asyncpg.connect(pg_dsn)
    await register_vector(conn)

    # Ensure schema
    from deltas.db import DDL_SQL, HNSW_INDEXES

    await conn.execute(DDL_SQL)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    for _name, ddl in HNSW_INDEXES:
        with contextlib.suppress(asyncpg.DuplicateObjectError, asyncpg.DuplicateTableError):
            await conn.execute(ddl)

    # Fetch all tags
    all_tags = _fetch_all_tags(sqlite_conn)

    # Count total
    total = sqlite_conn.execute("SELECT COUNT(*) FROM deltas").fetchone()[0]
    print(f"Migrating {total} deltas from {sqlite_path} to Postgres...")

    # Stream rows in batches
    cursor = sqlite_conn.execute(
        "SELECT id, timestamp, modality, content, embedding, provenance_embedding, "
        "source, media_hash, expires_at FROM deltas ORDER BY timestamp"
    )

    written = 0
    skipped = 0
    errors = 0
    batch: list[tuple] = []

    for row in cursor:
        delta_id = row["id"]
        tags = all_tags.get(delta_id, [])

        try:
            ts = _parse_ts(row["timestamp"])
            emb = _unpack_blob(row["embedding"])
            prov = _unpack_blob(row["provenance_embedding"])
            exp = _parse_ts(row["expires_at"]) if row["expires_at"] else None

            batch.append(
                (
                    delta_id,
                    ts,
                    row["modality"],
                    row["content"],
                    emb,
                    prov,
                    row["source"],
                    tags,
                    row["media_hash"],
                    exp,
                )
            )

            if len(batch) >= batch_size:
                inserted, skip, err = await _insert_batch(conn, batch)
                written += inserted
                skipped += skip
                errors += err
                batch = []

                if written % 5000 < batch_size:
                    print(f"  {written}/{total} ({written * 100 // total}%)")

        except Exception as e:
            errors += 1
            print(f"  Error on {delta_id}: {e}", file=sys.stderr)

    # Final batch
    if batch:
        inserted, skip, err = await _insert_batch(conn, batch)
        written += inserted
        skipped += skip
        errors += err

    # Verify
    pg_count = await conn.fetchval("SELECT COUNT(*) FROM deltas")
    pg_embedded = await conn.fetchval("SELECT COUNT(*) FROM deltas WHERE embedding IS NOT NULL")

    await conn.close()
    sqlite_conn.close()

    stats = {
        "sqlite_total": total,
        "pg_total": pg_count,
        "written": written,
        "skipped": skipped,
        "errors": errors,
        "pg_embedded": pg_embedded,
    }
    print(f"\nDone: {stats}")
    return stats


def _sanitize_text(text: str) -> str:
    """Strip null bytes that Postgres rejects in text columns."""
    return text.replace("\x00", "")


async def _insert_batch(conn: asyncpg.Connection, batch: list[tuple]) -> tuple[int, int, int]:
    """Insert a batch of rows. Returns (written, skipped, errors).

    Uses savepoints per row so one bad row doesn't poison the batch.
    """
    written = 0
    skipped = 0
    errors = 0

    async with conn.transaction():
        for row in batch:
            # Sanitize text fields (content is index 3)
            row_list = list(row)
            if isinstance(row_list[3], str):
                row_list[3] = _sanitize_text(row_list[3])
            sanitized = tuple(row_list)

            try:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO deltas (id, timestamp, modality, content, embedding,
                                            provenance_embedding, source, tags, media_hash,
                                            expires_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        *sanitized,
                    )
                written += 1
            except asyncpg.UniqueViolationError:
                skipped += 1
            except Exception as e:
                errors += 1
                print(f"  Row error ({sanitized[0]}): {e}", file=sys.stderr)

    return written, skipped, errors


def main():
    parser = argparse.ArgumentParser(description="Migrate delta store from SQLite to Postgres")
    parser.add_argument("--sqlite", required=True, help="Path to SQLite database")
    parser.add_argument("--pg", required=True, help="Postgres DSN")
    parser.add_argument("--batch-size", type=int, default=500, help="Rows per batch")
    args = parser.parse_args()

    stats = asyncio.run(migrate(args.sqlite, args.pg, args.batch_size))

    if stats["sqlite_total"] != stats["pg_total"]:
        print(
            f"\nWARNING: Count mismatch! SQLite={stats['sqlite_total']}, "
            f"Postgres={stats['pg_total']}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\nMigration complete. Counts match.")


if __name__ == "__main__":
    main()
