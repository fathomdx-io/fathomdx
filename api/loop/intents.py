"""Intents — the loop's pending-attention queue.

Every intent IS a delta in the puddle. Its delta-id becomes the
intent-id that witness outputs reference via `addresses:<id>` tags, so
queue resolution uses the same multi-claim pattern the witness already
uses for synthesis routing.

Intent kinds + their TTLs are below. An intent ages out of the queue
if witness hasn't addressed it within its TTL window. Question intents
get the durable Q-window so a slow deliberation doesn't drop the
user's question; resonance / pressure / drop-in are short — the
substrate that triggered them is itself short-lived.
"""

from __future__ import annotations

import json

from .puddle import puddle


# Rolling 48h horizon — gives the feed real substance, lets the loop
# resonate against a wider working window, and prevents Q/A from falling
# off-screen mid-thought. Everything in the puddle aspires to this same
# horizon; the per-kind table is kept as the single override surface in
# case a future kind wants something tighter (alerts that should fade
# fast, etc.) but for now every kind reaches for the rolling default.
Q_A_TTL_S = 48 * 60 * 60

INTENT_TTL_BY_KIND: dict[str, int] = {
    "question":    Q_A_TTL_S,
    "resonance":   Q_A_TTL_S,
    "pressure":    Q_A_TTL_S,
    "drop-in":     Q_A_TTL_S,
    "alert":       Q_A_TTL_S,
    "routine-due": Q_A_TTL_S,
    "reflection":  Q_A_TTL_S,
    "drift":       Q_A_TTL_S,
    "bridging":    Q_A_TTL_S,
}


CONVO = "grand"  # single-tenant grand convo; preserved for tag shape compatibility
CONVO_TAG = f"convo:{CONVO}"


async def write_intent(
    *,
    kind: str,
    content: str,
    payload: dict | None = None,
    extra_tags: list[str] | None = None,
    ttl_seconds: int | None = None,
    source: str = "intent-detector",
) -> dict:
    """Write an intent delta into the puddle.

    `content` is the human-readable summary (what voices and witness
    see inline). `payload` is structured kind-specific data — embedded
    as JSON in the content alongside the human summary so voices read
    plain text, not opaque blobs.
    """
    ttl = ttl_seconds if ttl_seconds is not None else INTENT_TTL_BY_KIND.get(kind, 5 * 60)
    tags = [CONVO_TAG, "intent", f"kind:{kind}"]
    if extra_tags:
        tags.extend(extra_tags)
    body = content
    if payload:
        body = f"{content}\n\n[intent-payload] {json.dumps(payload, ensure_ascii=False)}"
    return await puddle.write(
        content=body,
        tags=tags,
        source=source,
        ttl_seconds=ttl,
    )


def pending_intents(since_iso: str | None = None) -> list[dict]:
    """Intents with no covering witness output. Oldest-first.

    Outputs that resolve intents carry an `addressing-output` marker tag
    so we can find them with one tag query instead of scanning the
    whole puddle. Bare seed deltas (no `intent` tag, written by the
    composer before this module existed in the experiment) are treated
    as kind:question intents for back-compat.

    `since_iso` (optional): only return intents written at or after
    this timestamp. The supervisor passes its boot time so a restart
    starts with an empty queue rather than churning through stale
    legacy seeds.
    """
    intents = puddle.query(tags_include=[CONVO_TAG, "intent"], limit=100)
    seeds = puddle.query(tags_include=[CONVO_TAG, "seed"], limit=50)
    outputs = puddle.query(tags_include=[CONVO_TAG, "addressing-output"], limit=200)

    seen_ids: set[str] = set()
    candidates: list[dict] = []
    for d in intents + seeds:
        did = d.get("id") or ""
        if did and did not in seen_ids:
            seen_ids.add(did)
            candidates.append(d)

    if since_iso:
        candidates = [c for c in candidates if (c.get("timestamp") or "") >= since_iso]

    addressed: set[str] = set()
    for o in outputs:
        for tag in (o.get("tags") or []):
            if tag.startswith("addresses:"):
                addressed.add(tag.split(":", 1)[1])

    pending = [c for c in candidates if (c.get("id") or "") not in addressed]
    pending.sort(key=lambda d: d.get("timestamp") or "")
    return pending


def intent_kind(intent: dict) -> str:
    """Extract the kind:<x> tag. Returns 'unknown' for hand-written test
    deltas missing the kind tag."""
    for t in (intent.get("tags") or []):
        if t.startswith("kind:"):
            return t.split(":", 1)[1]
    return "unknown"
