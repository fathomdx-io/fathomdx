"""Contacts + handles registry — hard state for "who is talking to Fathom."

Contacts are the source of truth for person identity. Handles are how a
contact shows up across channels. See docs/contact-spec.md for the full
model and why this lives alongside the deltas table instead of being
derived from the lake.
"""

from __future__ import annotations

import asyncpg

from .store import _format_ts


def _row_to_contact(row: asyncpg.Record) -> dict:
    return {
        "slug": row["slug"],
        "display_name": row["display_name"],
        "role": row["role"],
        "notes": row["notes"],
        "created_at": _format_ts(row["created_at"]),
    }


def _row_to_handle(row: asyncpg.Record) -> dict:
    return {
        "contact_slug": row["contact_slug"],
        "channel": row["channel"],
        "identifier": row["identifier"],
        "created_at": _format_ts(row["created_at"]),
    }


class ContactsStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    # ── Contacts ─────────────────────────────────────────────────────────

    async def create(
        self,
        slug: str,
        display_name: str,
        role: str = "member",
        notes: str = "",
    ) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO contacts (slug, display_name, role, notes)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                slug,
                display_name,
                role,
                notes,
            )
            return _row_to_contact(row)

    async def get(self, slug: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM contacts WHERE slug = $1", slug)
            return _row_to_contact(row) if row else None

    async def list_all(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM contacts ORDER BY created_at")
            return [_row_to_contact(r) for r in rows]

    async def update(
        self,
        slug: str,
        *,
        display_name: str | None = None,
        role: str | None = None,
        notes: str | None = None,
    ) -> dict | None:
        sets = []
        vals: list = []
        i = 1
        if display_name is not None:
            sets.append(f"display_name = ${i}")
            vals.append(display_name)
            i += 1
        if role is not None:
            sets.append(f"role = ${i}")
            vals.append(role)
            i += 1
        if notes is not None:
            sets.append(f"notes = ${i}")
            vals.append(notes)
            i += 1
        if not sets:
            return await self.get(slug)

        vals.append(slug)
        sql = f"UPDATE contacts SET {', '.join(sets)} WHERE slug = ${i} RETURNING *"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, *vals)
            return _row_to_contact(row) if row else None

    async def delete(self, slug: str) -> bool:
        async with self._pool.acquire() as conn:
            status = await conn.execute("DELETE FROM contacts WHERE slug = $1", slug)
            return status.endswith(" 1")

    # ── Handles ──────────────────────────────────────────────────────────

    async def add_handle(
        self, contact_slug: str, channel: str, identifier: str
    ) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO handles (contact_slug, channel, identifier)
                VALUES ($1, $2, $3)
                RETURNING *
                """,
                contact_slug,
                channel,
                identifier,
            )
            return _row_to_handle(row)

    async def remove_handle(
        self, contact_slug: str, channel: str, identifier: str
    ) -> bool:
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                """
                DELETE FROM handles
                WHERE contact_slug = $1 AND channel = $2 AND identifier = $3
                """,
                contact_slug,
                channel,
                identifier,
            )
            return status.endswith(" 1")

    async def list_handles(self, contact_slug: str) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM handles WHERE contact_slug = $1 ORDER BY created_at",
                contact_slug,
            )
            return [_row_to_handle(r) for r in rows]

    async def resolve_handle(self, channel: str, identifier: str) -> str | None:
        """Return contact_slug for a (channel, identifier) pair, or None."""
        async with self._pool.acquire() as conn:
            slug = await conn.fetchval(
                "SELECT contact_slug FROM handles WHERE channel = $1 AND identifier = $2",
                channel,
                identifier,
            )
            return slug

    # ── Backfill ─────────────────────────────────────────────────────────

    async def backfill_contact_tag(
        self, contact_slug: str, filter_tags: list[str]
    ) -> dict:
        """Append `contact:<slug>` to every delta whose tags contain ANY of
        the filter tags and which has no `contact:` tag yet.

        Idempotent: running twice does nothing on the second pass.
        Returns counts for logging.
        """
        tag_to_add = f"contact:{contact_slug}"
        async with self._pool.acquire() as conn:
            candidates = await conn.fetchval(
                """
                SELECT COUNT(*) FROM deltas
                WHERE tags && $1
                  AND NOT EXISTS (
                    SELECT 1 FROM unnest(tags) t WHERE t LIKE 'contact:%'
                  )
                """,
                filter_tags,
            )
            updated = await conn.execute(
                """
                UPDATE deltas
                SET tags = array_append(tags, $2)
                WHERE tags && $1
                  AND NOT EXISTS (
                    SELECT 1 FROM unnest(tags) t WHERE t LIKE 'contact:%'
                  )
                """,
                filter_tags,
                tag_to_add,
            )
            affected = int(updated.split()[-1]) if updated else 0
        return {
            "candidates": int(candidates or 0),
            "updated": affected,
            "tag_added": tag_to_add,
            "filter_tags": filter_tags,
        }
