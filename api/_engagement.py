"""Engagement-snapshot helper.

When the user (or Fathom) writes an `affirms:<id>` / `refutes:<id>` /
`reply-to:<id>` delta against an observation, the engagement delta is
itself authored memory and persists forever — but the target it points
at may wilt on its TTL. To keep the engagement useful after the target
reaps, we snapshot the target's content (and media reference, if any)
into the engagement delta's body at write time.

This is the conforming mechanism for the third law in
`docs/explanation/what-sticks-and-what-wilts.md`: engagement is how
observation becomes authored, *without* mutating the original.

If the target fetch fails (already reaped, network blip, unknown id)
we still write the engagement — best-effort durable beats refusing to
record the intent. The footer notes the snapshot is missing.
"""

from __future__ import annotations

import logging

from . import delta_client

log = logging.getLogger(__name__)

# How much of the target's content we copy into the engagement delta.
# Long enough to preserve essentially every chat message and most feed
# cards; deltas larger than this (a vault chunk, a long article) are
# truncated with an ellipsis at the cut.
_SNAPSHOT_LIMIT = 4000


async def build_engagement_payload(
    target_id: str,
    reason: str,
) -> tuple[str, str | None]:
    """Fetch the target delta and build the engagement delta's body.

    Returns ``(content, media_hash)``:
      - ``content``: blockquoted snapshot of the target + provenance
        footer + the engager's reason. Stand-alone after the target reaps.
      - ``media_hash``: copied from the target if it had one. Lets the
        engagement re-reference the same image bytes without re-upload.

    Best-effort: on any fetch failure the engagement still writes, with
    just the reason and a footer noting the snapshot was unavailable.
    """
    target_content = ""
    target_source = "?"
    target_ts = ""
    media_hash: str | None = None
    fetch_ok = False

    try:
        target = await delta_client.get_delta(target_id)
        target_content = (target.get("content") or "").strip()
        target_source = target.get("source") or "?"
        target_ts = (target.get("timestamp") or "")[:16]
        media_hash = target.get("media_hash")
        fetch_ok = True
    except Exception:
        log.exception("engagement: failed to fetch target %s for snapshot", target_id)

    parts: list[str] = []

    if fetch_ok and target_content:
        snapshot = target_content[:_SNAPSHOT_LIMIT]
        if len(target_content) > _SNAPSHOT_LIMIT:
            snapshot += "…"
        # Blockquote each line so the snapshot is visually distinct from
        # the engager's reason underneath.
        quoted = "\n".join(f"> {line}" if line else ">" for line in snapshot.splitlines())
        parts.append(quoted)
        media_marker = " · [image]" if media_hash else ""
        parts.append(
            f"> — {target_source} · {target_ts} · {target_id[:8]}{media_marker}"
        )
    elif fetch_ok and media_hash:
        # Image-only target with no text body — keep the footer so the
        # engagement still names what it pointed at.
        parts.append(
            f"> — {target_source} · {target_ts} · {target_id[:8]} · [image]"
        )
    elif not fetch_ok:
        # Target unreachable — record that the snapshot is unavailable so
        # a reader can tell "no snapshot" apart from "target had no text".
        parts.append(f"> — target {target_id[:8]} unavailable at engagement time")

    if reason:
        if parts:
            parts.append("")
        parts.append(reason)

    return ("\n".join(parts).strip(), media_hash)
