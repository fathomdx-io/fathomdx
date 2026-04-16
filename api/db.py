"""Lake-backed sessions (matching loop-api pattern) + Postgres sources."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta

import asyncpg
import httpx

from . import delta_client
from .settings import settings
from .slug import generate_slug

_pool: asyncpg.Pool | None = None

# Match loop-api conventions exactly
LAKE_CHAT_TAG = "fathom-chat"
LAKE_CHAT_SOURCE = "fathom-chat"
LAKE_SESSION_LIST_WINDOW_DAYS = 30
LAKE_SESSION_LIST_LIMIT = 1000

DDL = """
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    name        TEXT NOT NULL,
    config      JSONB NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _id() -> str:
    return uuid.uuid4().hex[:12]


async def init_pool():
    global _pool
    if _pool is not None:
        return
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=5,
    )
    async with _pool.acquire() as conn:
        await conn.execute(DDL)


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    assert _pool is not None, "call init_pool() first"
    return _pool


# ── Sessions (lake-backed, matching loop-api) ───
#
# Sessions use combinatronic slugs (adj-adj-animal).
# Deltas are tagged: fathom-chat, chat:{slug}, role (user/assistant)
# Session names: fathom-chat, chat:{slug}, chat-name
# This is the SAME format as loop-api/server.py so sessions
# created by either surface are visible in both.


def _extract_chat_slug(tags: list[str]) -> str | None:
    """Return the chat:<slug> slug from a tag list."""
    for tag in tags:
        if tag.startswith("chat:"):
            return tag[5:]
    return None


async def create_session(title: str = "New session") -> dict:
    """Mint a fresh slug. No delta written until first message (or name)."""
    # Generate a unique slug by checking for collisions in the lake
    c = await delta_client._get()
    for _ in range(10):
        slug = generate_slug()
        r = await c.get("/deltas", params={"tags_include": f"chat:{slug}", "limit": 1})
        if r.status_code == 200 and not r.json():
            break
    return {"id": slug, "title": title, "created_at": datetime.now(timezone.utc).isoformat()}


async def list_sessions(limit: int = 50) -> list[dict]:
    """Aggregate fathom-chat deltas into session list — same as loop-api.

    Each session: {id (slug), title (name), first_seen, last_seen, delta_count, preview}.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=LAKE_SESSION_LIST_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    results = await delta_client.query(
        tags_include=[LAKE_CHAT_TAG],
        time_start=since,
        limit=LAKE_SESSION_LIST_LIMIT,
    )

    buckets: dict[str, dict] = {}
    for d in results:
        tags = d.get("tags") or []
        slug = _extract_chat_slug(tags)
        if not slug:
            slug = "legacy"
        ts = d.get("timestamp") or ""
        b = buckets.setdefault(slug, {
            "id": slug,
            "title": None,
            "_name_ts": "",
            "created_at": ts,
            "updated_at": ts,
            "delta_count": 0,
            "preview": "",
            "_preview_ts": "",
        })
        b["delta_count"] += 1
        if ts and ts < (b["created_at"] or ts):
            b["created_at"] = ts
        if ts and ts > (b["updated_at"] or ""):
            b["updated_at"] = ts

        content = d.get("content") or ""

        if "chat-name" in tags and ts >= b["_name_ts"]:
            b["title"] = content.strip() or None
            b["_name_ts"] = ts
        elif "user" in tags and ts >= b["_preview_ts"]:
            b["preview"] = content[:140]
            b["_preview_ts"] = ts

    # Clean up internal keys and sort
    legacy = buckets.get("legacy")
    if legacy and not legacy["title"]:
        legacy["title"] = "before sessions"

    sessions = []
    for b in buckets.values():
        sessions.append({
            "id": b["id"],
            "title": b["title"] or b["id"],
            "created_at": b["created_at"],
            "updated_at": b["updated_at"],
            "delta_count": b["delta_count"],
            "preview": b.get("preview", ""),
        })
    sessions.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return sessions[:limit]


async def get_session(session_id: str) -> dict | None:
    """Get session metadata by querying its chat-name delta."""
    results = await delta_client.query(
        tags_include=[LAKE_CHAT_TAG, f"chat:{session_id}"],
        limit=1,
    )
    if not results:
        # Session exists but has no deltas yet — still valid if just created
        return {"id": session_id, "title": session_id, "created_at": "", "updated_at": ""}
    d = results[0]
    # Find the name
    name_results = await delta_client.query(
        tags_include=[LAKE_CHAT_TAG, f"chat:{session_id}", "chat-name"],
        limit=1,
    )
    title = session_id
    if name_results:
        title = name_results[0].get("content", session_id).strip() or session_id
    return {
        "id": session_id,
        "title": title,
        "created_at": d.get("timestamp", ""),
        "updated_at": d.get("timestamp", ""),
    }


async def update_session(session_id: str, title: str) -> dict | None:
    """Rename by writing a chat-name delta — latest wins."""
    await delta_client.write(
        content=title,
        tags=[LAKE_CHAT_TAG, f"chat:{session_id}", "chat-name"],
        source=LAKE_CHAT_SOURCE,
    )
    return {"id": session_id, "title": title}


async def delete_session(session_id: str) -> bool:
    """Tombstone a session."""
    await delta_client.write(
        content=f"session deleted: {session_id}",
        tags=[LAKE_CHAT_TAG, f"chat:{session_id}", "chat-deleted"],
        source=LAKE_CHAT_SOURCE,
    )
    return True


async def touch_session(session_id: str):
    """No-op — updated_at is derived from latest delta timestamp."""
    pass


# ── Messages (lake-backed, matching loop-api tags) ──


async def add_message(
    session_id: str,
    role: str,
    content: str | None = None,
    tool_calls: str | None = None,
    tool_call_id: str | None = None,
) -> str:
    """Write a chat message as a delta — same tag format as loop-api."""
    tags = [LAKE_CHAT_TAG, f"chat:{session_id}", role]
    result = await delta_client.write(
        content=content or "",
        tags=tags,
        source=LAKE_CHAT_SOURCE,
    )
    return result.get("id", "")


async def get_messages(session_id: str, limit: int = 200) -> list[dict]:
    """Load session history from the lake."""
    results = await delta_client.query(
        tags_include=[LAKE_CHAT_TAG, f"chat:{session_id}"],
        limit=limit,
    )
    # Newest-first from API, reverse for chronological
    results.reverse()
    messages = []
    for d in results:
        tags = d.get("tags", [])
        # Skip non-message deltas (chat-name, chat-deleted)
        if "chat-name" in tags or "chat-deleted" in tags:
            continue
        role = "user" if "user" in tags else "assistant" if "assistant" in tags else None
        if not role:
            continue
        msg = {
            "id": d.get("id"),
            "role": role,
            "content": d.get("content"),
            "created_at": d.get("timestamp"),
        }
        if d.get("media_hash"):
            msg["media_hash"] = d["media_hash"]
        messages.append(msg)
    return messages


# ── Sources (Postgres) ──────────────────────────


async def create_source(type: str, name: str, config: dict | None = None) -> dict:
    sid = _id()
    now = datetime.now(timezone.utc)
    cfg = json.dumps(config or {})
    await pool().execute(
        "INSERT INTO sources (id, type, name, config, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4::jsonb, $5, $5)",
        sid, type, name, cfg, now,
    )
    return {"id": sid, "type": type, "name": name, "config": config or {}, "state": "active"}


async def list_sources() -> list[dict]:
    rows = await pool().fetch(
        "SELECT id, type, name, config, state, created_at, updated_at "
        "FROM sources ORDER BY created_at ASC",
    )
    return [dict(r) for r in rows]


async def update_source(source_id: str, state: str | None = None, config: dict | None = None) -> dict | None:
    now = datetime.now(timezone.utc)
    row = await pool().fetchrow("SELECT * FROM sources WHERE id = $1", source_id)
    if not row:
        return None
    new_state = state or row["state"]
    new_config = json.dumps(config) if config else row["config"]
    updated = await pool().fetchrow(
        "UPDATE sources SET state = $1, config = $2::jsonb, updated_at = $3 "
        "WHERE id = $4 RETURNING id, type, name, config, state, created_at, updated_at",
        new_state, new_config if isinstance(new_config, str) else json.dumps(new_config), now, source_id,
    )
    return dict(updated) if updated else None


async def delete_source(source_id: str) -> bool:
    result = await pool().execute("DELETE FROM sources WHERE id = $1", source_id)
    return result == "DELETE 1"
