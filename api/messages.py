"""Direct messages — Fathom reaching out to a contact.

Distinct from the chat reply path. The reply path writes
participant:fathom deltas as part of the chat turn loop in response to a
user message. `send_message` here is for the OTHER direction: Fathom
proactively writes a delta addressed to a contact, so it surfaces in the
contact's header alerts even when they aren't currently watching.

The default destination is the contact-pair direct thread,
`chat:direct:<recipient>`. This thread is implicit — no roster, no
allocation, no minting. Just a stable slug derived from the recipient.
Cold reach-outs from routines or MCP/CLI calls land there; followups
(when the user replies in the dashboard) extend the same thread.

Callers may override `session_slug` to drop the message into an existing
session — useful when the conversation already has a topic discriminator.
The message gets BOTH the override session tag and the direct-thread tag,
so it's reachable from either view.
"""

from __future__ import annotations

from datetime import UTC, datetime

from . import delta_client

LAKE_CHAT_TAG = "fathom-chat"
LAKE_CHAT_SOURCE = "fathom-chat"


def direct_thread_slug(recipient_slug: str) -> str:
    """Stable slug for the Fathom↔contact direct thread."""
    return f"direct:{recipient_slug}"


async def send_message(
    *,
    recipient_slug: str,
    body: str,
    writer_slug: str = "fathom",
    session_slug: str | None = None,
) -> dict:
    """Write a chat delta addressed to a contact.

    Tags:
      fathom-chat              — session membership marker
      chat:direct:<recipient>  — contact-pair thread (always present)
      chat:<session>           — optional override discriminator
      participant:<writer>     — who wrote it (defaults to "fathom")
      assistant                — legacy role tag for back-compat with get_messages
      contact:<recipient>      — correspondence anchor
      for:<recipient>          — addressing tag, drives header alerts

    `delta_client.write` is in-process and bypasses the /v1/deltas
    contact-tag gate, which is correct here: the LLM is writing on
    behalf of itself and naming the recipient explicitly.
    """
    recipient = (recipient_slug or "").strip()
    text = (body or "").strip()
    if not recipient:
        raise ValueError("recipient_slug required")
    if not text:
        raise ValueError("body required")

    direct_slug = direct_thread_slug(recipient)
    tags = [
        LAKE_CHAT_TAG,
        f"chat:{direct_slug}",
        f"participant:{writer_slug or 'fathom'}",
        "assistant",
        f"contact:{recipient}",
        f"for:{recipient}",
    ]
    if session_slug and session_slug != direct_slug:
        tags.append(f"chat:{session_slug}")

    written = await delta_client.write(
        content=text,
        tags=tags,
        source=LAKE_CHAT_SOURCE,
    )
    return {
        "ok": True,
        "id": written.get("id"),
        "session_id": direct_slug,
        "recipient": recipient,
    }


async def recent_dm_thread(recipient_slug: str, *, limit: int = 3) -> list[dict]:
    """Last N Fathom-written direct messages to this contact, newest first.

    Returns each as `{timestamp, content}` — the shape the synthesis
    prompt builder needs. Empty contact slug or query failure returns
    an empty list (not an error). Only `participant:fathom` deltas are
    pulled; user replies in the same thread aren't included because
    cadence/dedup is about Fathom's own outgoing voice.
    """
    if not recipient_slug:
        return []
    try:
        results = await delta_client.query(
            tags_include=[
                LAKE_CHAT_TAG,
                f"chat:{direct_thread_slug(recipient_slug)}",
                "participant:fathom",
            ],
            limit=limit,
        )
    except Exception:
        return []
    return [
        {"timestamp": d.get("timestamp") or "", "content": d.get("content") or ""} for d in results
    ]


def _humanize_age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


async def dm_context_block(recipient_slug: str, *, limit: int = 3) -> str:
    """Prompt block describing recent direct messages to a contact.

    Injected into the synthesis prompts (feed-loop card production, and
    eventually the feed-orient crystal) so the model can: dedup against
    things it already said, write smooth segues into ongoing threads,
    and self-regulate cadence by seeing how recently it last reached out.

    The block also documents the routing capability — `direct: true` on
    a card output sends it as a DM instead of writing a feed-card delta.
    Without this block in the prompt the model has no reason to know
    that option exists.

    Returns "" for an empty recipient (caller decides whether to inline
    or skip the block in that case).
    """
    if not recipient_slug:
        return ""
    history = await recent_dm_thread(recipient_slug, limit=limit)
    name = recipient_slug

    lines = [
        f"=== DIRECT MESSAGES TO {name} ===",
        (
            "You can route any single output as a direct message to "
            f"{name} by setting `direct: true` on it (with the message text "
            "in `body`). Use this rarely — only for synthesis that genuinely "
            "warrants direct attention. Most outputs should be feed cards. "
            "The bell only matters if it's earned."
        ),
        "",
    ]

    if not history:
        lines.append(f"You haven't sent any direct messages to {name} recently.")
        return "\n".join(lines)

    now = datetime.now(UTC)
    lines.append(f"Recent direct messages you sent to {name} (newest first):")
    for d in history:
        ts = d.get("timestamp") or ""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = _humanize_age((now - dt).total_seconds())
        except Exception:
            age = ts or "unknown"
        content = (d.get("content") or "").strip().replace("\n", " ")
        if len(content) > 160:
            content = content[:160] + "…"
        lines.append(f"  [{age}] {content}")
    lines.append("")

    # Cadence note keyed off the most recent message — the soft pressure
    # the human asked for, scaled to recency. No hard cooldown; the model
    # decides whether the new output clears its own bar against the
    # visible thread above.
    try:
        ts = history[0].get("timestamp") or ""
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age_min = (now - dt).total_seconds() / 60
    except Exception:
        age_min = 1e9

    if age_min < 5:
        lines.append(
            f"Cadence: you reached out to {name} less than 5 minutes ago. If "
            "anything you'd send now would feel redundant or only marginally "
            "different, route it as a feed card instead."
        )
    elif age_min < 60:
        lines.append(
            f"Cadence: your most recent message to {name} was about "
            f"{int(age_min)} minutes ago. Don't spam — only set `direct: true` "
            "when this output genuinely warrants its own message."
        )
    else:
        lines.append(
            f"Cadence is fine. Still be selective about what gets sent to {name} "
            "directly versus published as a feed card."
        )
    return "\n".join(lines)
