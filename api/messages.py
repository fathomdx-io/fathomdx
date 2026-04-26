"""Direct messages — Fathom reaching out to a contact.

Distinct from the chat listener's reply path. The listener writes
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
