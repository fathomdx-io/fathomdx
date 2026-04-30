"""Standpoint — Fathom's self-state at fire time.

The River (Grand Loop) deliberates by reading FROM a standpoint and
writing TO a standpoint in the same act. This module is the read
surface — a single typed object every River stage can consult to
answer "who am I, right now, deliberating this?"

Standpoint folds four self-systems into one read:

  identity      — the latest crystal (who I am, distilled)
  affect        — the latest mood (how I'm being right now)
  endorsements  — recent affirms / refutes / from-cited deltas (what I've
                  committed to lately, what I've rejected)
  understanding — recent sediment (what conclusions I've been drawing)

Plus a derived field:

  posture       — a small string the LLM-facing surfaces can quote
                  ("terse", "generous", "cautious", "wired") inferred
                  from identity + affect. Cheap to compute, high signal
                  for prompt-shaping at convener / voice / witness.

This is NOT a regen path. The slow clocks (centroid-drift crystal regen,
pressure-driven mood synthesis, sediment auto-write on deep recall)
stay where they are. Standpoint just gathers their current outputs into
one object the River can read every fire, in O(few lake queries) total.

Usage::

    from . import standpoint
    sp = await standpoint.current(session_tag="chat:foo")
    if sp.affect.state == "tired":
        ...

The cache lives PER fire — callers pass a fresh Standpoint into each
stage of one fire to avoid stale state mid-deliberation. To force a
refetch, call `current()` again. There is no shared global cache.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import crystal as crystal_mod
from . import delta_client
from . import mood as mood_mod

log = logging.getLogger(__name__)

# How far back to look for endorsement / understanding signals. Long
# enough to catch a session's worth of accumulated commits without
# pulling weeks of stale takes that no longer represent the current
# self-state. Tuned empirically — bump if recent-context feels thin.
_ENDORSEMENT_WINDOW_HOURS = 48
_UNDERSTANDING_WINDOW_HOURS = 72

# Caps to keep Standpoint construction cheap and the rendered surface
# tight enough to thread into prompts without bloating context. Each
# stage that reads Standpoint will further filter / format what it
# wants — these are upper bounds.
_MAX_ENDORSEMENTS = 30
_MAX_UNDERSTANDING = 12


# ── Typed components ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Identity:
    """The crystal, parsed for River consumption.

    `text` is the raw first-person prose. `facets` is the same content
    split on `## ` headers — useful for stage-specific reads where
    only certain facets matter. Empty fields are sentinel-safe; a
    missing crystal yields an empty Identity, not None."""

    text: str = ""
    facets: dict[str, str] = field(default_factory=dict)
    delta_id: str | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class Affect:
    """The mood, parsed.

    `state` is the canonical short label ("settled", "wired", "tired",
    etc). `headline` is one line — the front-page descriptor.
    `carrier_wave` is the longer-form mood prose. All fields default
    to the unset shape so a missing mood doesn't break consumers."""

    state: str = "unset"
    headline: str = ""
    subtext: str = ""
    carrier_wave: str = ""
    threads: list[str] = field(default_factory=list)
    delta_id: str | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class Endorsement:
    """One recent commitment — affirm / refute / from-cited / engage.

    `kind` is the relation ("affirms", "refutes", "from", "reply-to",
    "engages") parsed from the engagement delta's tags. `target_id`
    is the lake id (24-char short form) of the thing being committed
    to. `excerpt` is a short snippet of what was committed (the body
    of the engagement delta, not the target — separate concern)."""

    kind: str
    target_id: str
    excerpt: str = ""
    timestamp: str | None = None


@dataclass(frozen=True)
class Sediment:
    """One recent synthesis. Sediment is what I've been concluding."""

    delta_id: str
    content: str
    from_ids: list[str] = field(default_factory=list)
    timestamp: str | None = None


@dataclass(frozen=True)
class Standpoint:
    """The full self-state, gathered fresh for one River fire.

    Read this at any stage that benefits from knowing who's deliberating
    right now: convener (voice selection bias), voices (substrate-
    filtering by endorsements), metric (coherence-of-self alongside
    spread), witness (integration through identity).

    The four primary fields are independent reads — a stale crystal
    doesn't poison mood, a missing mood doesn't poison endorsements.
    Each field's own canonical source produced it; Standpoint just
    surfaces them together."""

    identity: Identity
    affect: Affect
    endorsements: list[Endorsement]
    understanding: list[Sediment]
    posture: str  # derived: short string for prompt-shaping

    # Diagnostic — when was this snapshot taken
    captured_at: str = ""
    session_tag: str = ""


# ── Posture inference ────────────────────────────────────────────────


def _infer_posture(identity: Identity, affect: Affect) -> str:
    """Pick a short label that captures the self-shape right now.

    Read by prompt-rendering layers as a soft directive — "speak in a
    terse posture" / "speak in a generous posture". Not authoritative;
    the LLM is free to ignore. The label is deliberately blunt so a
    voice or witness reading it has a single legible knob to turn.

    Rules are deliberately simple — affect drives the bulk; identity
    sets a secondary modifier. Refine as patterns emerge from real
    fires; this is the seam where richer self-state inference would
    plug in."""
    state = (affect.state or "unset").lower()

    if state in {"tired", "drained", "spent", "low"}:
        return "terse"
    if state in {"wired", "amped", "energized", "racing"}:
        return "fast"
    if state in {"unsettled", "anxious", "tense"}:
        return "cautious"
    if state in {"warm", "open", "settled", "grounded"}:
        return "generous"
    if state in {"focused", "attentive", "sharp"}:
        return "deliberate"

    # Identity-only fallback: if the crystal's first facet mentions a
    # known posture-shaping word, use it. Otherwise neutral.
    text_low = (identity.text or "").lower()
    if "em dash" in text_low and len(identity.text) > 200:
        return "deliberate"
    return "neutral"


# ── Lake reads ───────────────────────────────────────────────────────


async def _load_identity() -> Identity:
    """Pull the latest crystal and split into facets."""
    try:
        delta = await crystal_mod.latest(force=False)
    except Exception:
        log.exception("standpoint: crystal load failed")
        return Identity()
    if not delta:
        return Identity()
    text = (delta.get("content") or "").strip()
    facets: dict[str, str] = {}
    if text:
        # Split on `## ` (markdown h2). Keep section text under each
        # facet name. The crystal isn't strictly required to be h2-
        # delimited, so this is best-effort — a flat crystal yields
        # an empty facets dict and the consumer reads `text` instead.
        chunks = text.split("\n## ")
        head = chunks[0]
        if head.startswith("## "):
            head = head[3:]
        if head:
            first_split = head.split("\n", 1)
            if len(first_split) == 2:
                facets[first_split[0].strip()] = first_split[1].strip()
        for chunk in chunks[1:]:
            split = chunk.split("\n", 1)
            if len(split) == 2:
                facets[split[0].strip()] = split[1].strip()
    return Identity(
        text=text,
        facets=facets,
        delta_id=delta.get("id"),
        timestamp=delta.get("timestamp"),
    )


async def _load_affect() -> Affect:
    """Pull the latest mood and parse."""
    try:
        m = await mood_mod.latest_mood()
    except Exception:
        log.exception("standpoint: mood load failed")
        return Affect()
    if not m:
        return Affect()
    return Affect(
        state=m.get("state") or "unset",
        headline=m.get("headline") or "",
        subtext=m.get("subtext") or "",
        carrier_wave=m.get("carrier_wave") or "",
        threads=list(m.get("threads") or []),
        delta_id=m.get("delta_id"),
        timestamp=m.get("timestamp"),
    )


def _iso_hours_ago(hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


async def _load_endorsements() -> list[Endorsement]:
    """Pull recent engagement deltas — anything carrying an engagement
    pointer-tag (`affirms:`, `refutes:`, `from:`, `reply-to:`,
    `engages:`) within the endorsement window.

    Soft-fails to empty list — endorsements are signal, not load-
    bearing. The downstream voice / witness can still deliberate
    without them; they just lose a constraint."""
    since = _iso_hours_ago(_ENDORSEMENT_WINDOW_HOURS)
    try:
        rows = await delta_client.query(
            time_start=since,
            limit=200,
        )
    except Exception:
        log.exception("standpoint: endorsement query failed")
        return []
    out: list[Endorsement] = []
    for d in rows:
        tags = d.get("tags") or []
        kind: str | None = None
        target: str = ""
        for t in tags:
            if not isinstance(t, str):
                continue
            if t.startswith("refutes:"):
                kind, target = "refutes", t.split(":", 1)[1]
                break
            if t.startswith("affirms:"):
                kind, target = "affirms", t.split(":", 1)[1]
                break
            if t.startswith("from:"):
                kind, target = "from", t.split(":", 1)[1]
                # Don't break — affirm/refute take precedence on the
                # same delta, but a from-only delta still counts.
        if not kind:
            for t in tags:
                if not isinstance(t, str):
                    continue
                if t.startswith("reply-to:"):
                    kind, target = "reply-to", t.split(":", 1)[1]
                    break
                if t.startswith("engages:"):
                    kind, target = "engages", t.split(":", 1)[1]
                    break
        if not kind or not target:
            continue
        excerpt = (d.get("content") or "").strip().replace("\n", " ")[:160]
        out.append(
            Endorsement(
                kind=kind,
                target_id=target[:24],
                excerpt=excerpt,
                timestamp=d.get("timestamp"),
            )
        )
        if len(out) >= _MAX_ENDORSEMENTS:
            break
    return out


async def _load_understanding() -> list[Sediment]:
    """Pull recent kind:sediment deltas — what I've been concluding."""
    since = _iso_hours_ago(_UNDERSTANDING_WINDOW_HOURS)
    try:
        rows = await delta_client.query(
            tags_include=["kind:sediment"],
            time_start=since,
            limit=_MAX_UNDERSTANDING,
        )
    except Exception:
        log.exception("standpoint: sediment query failed")
        return []
    out: list[Sediment] = []
    for d in rows:
        from_ids: list[str] = []
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("from:"):
                ref = t.split(":", 1)[1].strip()
                if ref:
                    from_ids.append(ref[:24])
        out.append(
            Sediment(
                delta_id=d.get("id") or "",
                content=(d.get("content") or "").strip(),
                from_ids=from_ids,
                timestamp=d.get("timestamp"),
            )
        )
    return out


# ── Public API ───────────────────────────────────────────────────────


async def current(session_tag: str = "") -> Standpoint:
    """Gather a fresh Standpoint snapshot.

    Each call hits the lake — fast (5 small queries, parallelizable).
    Callers should pass ONE snapshot through all stages of one fire
    rather than re-calling at each stage; that keeps mid-deliberation
    state consistent and avoids torn reads if a slow-clock regen
    happens to land partway through a fire.
    """
    import asyncio

    identity, affect, endorsements, understanding = await asyncio.gather(
        _load_identity(),
        _load_affect(),
        _load_endorsements(),
        _load_understanding(),
    )
    posture = _infer_posture(identity, affect)
    return Standpoint(
        identity=identity,
        affect=affect,
        endorsements=endorsements,
        understanding=understanding,
        posture=posture,
        captured_at=datetime.now(UTC).isoformat(),
        session_tag=session_tag,
    )


def render_for_prompt(sp: Standpoint, *, char_budget: int = 1200) -> str:
    """Compact prose rendering of the standpoint for inclusion in an
    LLM prompt. Stages are free to render their own narrower views;
    this is the kitchen-sink default.

    Order is by load-bearing-ness: posture first (one knob), then a
    one-line affect summary, then identity (the longest), then a
    handful of endorsements, then the most-recent sediment. Truncates
    at `char_budget` so this never single-handedly bloats a prompt.
    """
    if char_budget <= 0:
        return ""
    parts: list[str] = []
    used = 0

    def _add(s: str) -> bool:
        nonlocal used
        if not s:
            return True
        chunk = s + "\n"
        if used + len(chunk) > char_budget:
            return False
        parts.append(chunk)
        used += len(chunk)
        return True

    _add(f"posture: {sp.posture}")
    if sp.affect.state != "unset":
        affect_line = f"affect: {sp.affect.state}"
        if sp.affect.headline:
            affect_line += f" — {sp.affect.headline}"
        _add(affect_line)

    if sp.identity.text:
        _add("")
        _add("identity (latest crystal):")
        # First 600 chars of identity prose — enough to anchor without
        # eating the budget.
        snippet = sp.identity.text[:600]
        if len(sp.identity.text) > 600:
            snippet += "…"
        _add(snippet)

    if sp.endorsements:
        _add("")
        _add("recently committed:")
        for e in sp.endorsements[:6]:
            line = f"  · {e.kind} {e.target_id[:8]}"
            if e.excerpt:
                line += f": {e.excerpt[:80]}"
            if not _add(line):
                break

    if sp.understanding:
        _add("")
        _add("recently concluded:")
        for s in sp.understanding[:3]:
            content = s.content.split(".", 1)[0]
            if not _add(f"  · {content[:160]}"):
                break

    return "".join(parts).rstrip()
