"""Lake-backed sessions for the OpenAI-compat surface.

The dashboard's session paradigm is retired — clicking feed cards now
writes engagement deltas (`reply-to:<id>`) that the Grand Loop processes
as intents. Sessions persist only for `/v1/chat/completions` (OpenAI-
shape clients), where the fathom-chat listener picks up `participant:user`
deltas and writes back `participant:fathom` replies.

Tags written here:
- fathom-chat (LAKE_CHAT_TAG) — every session-scoped delta
- chat:<slug> — groups deltas by session
- participant:user / participant:fathom — turn role
- contact:<slug>, for:<slug> — addressing for multi-contact installs
"""

from __future__ import annotations

from datetime import UTC, datetime

from . import delta_client
from ._tags import tag_suffix
from .slug import generate_slug


LAKE_CHAT_TAG = "fathom-chat"
LAKE_CHAT_SOURCE = "fathom-chat"


def _extract_chat_slug(tags: list[str]) -> str | None:
    """Return the chat:<slug> slug from a tag list."""
    return tag_suffix(tags, "chat:")


async def create_session(title: str = "New session") -> dict:
    """Mint a fresh slug. No delta written until first message (or name)."""
    c = await delta_client._get()
    for _ in range(10):
        slug = generate_slug()
        r = await c.get("/deltas", params={"tags_include": f"chat:{slug}", "limit": 1})
        if r.status_code == 200 and not r.json():
            break
    return {"id": slug, "title": title, "created_at": datetime.now(UTC).isoformat()}


async def get_session(session_id: str) -> dict | None:
    """Fetch session metadata (just the bare row — title is the slug)."""
    results = await delta_client.query(
        tags_include=[LAKE_CHAT_TAG, f"chat:{session_id}"],
        limit=1,
    )
    if not results:
        # Fresh session, no deltas yet.
        return {"id": session_id, "title": session_id, "created_at": "", "updated_at": ""}
    d = results[0]
    return {
        "id": session_id,
        "title": session_id,
        "created_at": d.get("timestamp", ""),
        "updated_at": d.get("timestamp", ""),
    }


async def add_message(
    session_id: str,
    role: str,
    content: str | None = None,
    tool_calls: str | None = None,
    tool_call_id: str | None = None,
    extra_tags: list[str] | None = None,
    contact_slug: str | None = None,
    media_hash: str | None = None,
) -> str:
    """Write a chat message as a delta (used by /v1/chat/completions).

    `contact_slug` marks correspondence: for user messages it's the
    author; for assistant messages it's the addressee. Adds for:<slug>
    on assistant deltas so header alerts route correctly.
    """
    participant_tag = {
        "user": "participant:user",
        "assistant": "participant:fathom",
    }.get(role)
    tags = [LAKE_CHAT_TAG, f"chat:{session_id}", role]
    if participant_tag:
        tags.append(participant_tag)
    if contact_slug:
        tags.append(f"contact:{contact_slug}")
        if role == "assistant":
            tags.append(f"for:{contact_slug}")
    if extra_tags:
        tags.extend(extra_tags)
    result = await delta_client.write(
        content=content or "",
        tags=tags,
        source=LAKE_CHAT_SOURCE,
        media_hash=media_hash,
    )
    return result.get("id", "")
