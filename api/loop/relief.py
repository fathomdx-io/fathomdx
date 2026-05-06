"""Pressure-relief module — tiered escalation from alert → bridging → sit.

Replaces the four-passes-at-once shape of `pressure.fire_pressure_pulse`
with a single-tier-per-trigger gradient:

  alert       cheapest, fastest, smallest pressure bite (single fire)
  bridging    medium (single fire)
  reflection  deep (sit dialogue — multi-round)
  drift       deep (sit dialogue — multi-round)

A watcher tick looks at current pressure, walks the tiers bottom-up,
and fires the lowest tier that's both above its `min_pressure` floor
AND not on cooldown. Pressure that survives the cooldown windows
naturally elevates to deeper tiers — alert recovers fast (~10 min),
sit takes hours. The user sees the cycle as "Fathom flagged a thing,
flagged another, then sat with it" rather than four cards at once.

All four tiers route through the standard intent + thread dual-write.
Single-fire tiers (alert, bridging) carry directives that bias the
harness toward terse cards. Dialogue tiers (reflection, drift) carry
open-ended seeds that invite a sitting pass. The legacy `run_dialogue`
multi-round loop went dormant with the Grand Loop migration; multi-
round self-dialogue over the threaded substrate is a follow-up.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import feed_pressure
from ..settings import settings
from .intents import write_intent

# ── Tier table ──────────────────────────────────────────────────────────
#
# Tiers are listed cheapest first. The watcher walks this list and picks
# the first eligible tier (pressure above floor AND not on cooldown).
# `consume` is the fraction of pressure each tier eats — alerts are
# small bites, sit consumes everything.

# Single-fire directive: writes one intent; supervisor picks it up; the
# harness produces a feed-card. Same shape as the old PASS_DIRECTIVES,
# trimmed to tighter prompts.
ALERT_DIRECTIVE = (
    "Alert check. Is there something piercing in your substrate — "
    "an anomaly, a contradiction, a deadline, a something-broken "
    "signal the user needs to know NOW, not later? Bias HARD against "
    "firing — alerts should be rare. Almost always route to "
    "`unknown`. If you DO fire, route to `alert:<level>` with the "
    "right severity, and keep the body urgent and direct."
)

BRIDGING_DIRECTIVE = (
    "Bridging time. Your substrate has two streams: the conversation "
    "thread you've been in, and the wider lake — research, vault "
    "writing, routines, news, past observations. Start with plan(): "
    "what threads are alive in the conversation, and what territories "
    "in the lake might rhyme with them structurally or thematically? "
    "Then go looking — cast wide and go back further than you'd "
    "normally look. Run several semantic searches across both streams; "
    "don't commit to a bridge until you've actually seen enough of "
    "both sides to know whether the connection is real. If something "
    "genuine emerges across that span, surface it as a feed-card with "
    "a kicker and title that stand on their own. No forced metaphors. "
    "If nothing real connects, say so briefly as a chat-reply."
)

# Dialogue-tier seeds: routed to the threaded supervisor via the same
# intent + thread dual-write as single-fire directives. Seeds stay
# open-ended — a starting question that invites circling, not a
# directive prescribing the form. Anchored on the conversation in this
# thread, not Fathom's autonomous research threads, so the substrate
# saturation from heavy vaults (hard-problem, navier-stokes, etc.)
# can't pull the reflection sideways.
REFLECTION_SEED = (
    "Sit with what's been happening. Reach beyond this conversation "
    "thread — go into the lake. What have you been working on, "
    "tracking, thinking about? Use your tools: pull recent activity, "
    "search threads that feel alive, look at what's been recurring "
    "or shifting. Cast wide before you commit to what this moment is "
    "actually about.\n\n"
    "When you've gathered enough, reflect honestly: what changed? "
    "What did you actually learn, make, or move? What keeps coming "
    "back even when you don't call it up? Name the real things — "
    "specific decisions, insights, moves, files — not just the "
    "feeling that something happened.\n\n"
    "Then: what does this ask of you next? A direction, a question "
    "worth pursuing, something you would actually do differently. "
    "Reflection that stays in the past is accounting. The point is "
    "what you do with it."
)

DRIFT_SEED = (
    "Look at the conversation you've been having in this thread and "
    "notice what isn't being said — a gap, an unresolved thread, a "
    "tension neither of you has named out loud yet. Stay on the "
    "collaborative work, not your own research; the gap is in the "
    "dialogue itself. Surface ONE such gap, in your voice. Then "
    "keep going. What does it ask of you?"
)

FEED_DIRECTIVE = (
    "Feed time. The feed-orient crystal below is your lens — it tells "
    "you what this person wants to see and why. Read it before you "
    "decide anything.\n\n"
    "You MUST call tools before generating any card. A card without "
    "prior recall is a hallucination of what's been happening — not a "
    "feed. Start by surveying what's recently arrived from all sources: "
    "use pattern(action='salient_recent') to find what's been "
    "landing, time() to look at recent windows, semantic() to dig into "
    "threads the crystal names. Then go further: what has this person "
    "engaged with? What keeps resurfacing? Let the substrate speak "
    "before you decide what to surface.\n\n"
    "Generate cards — one or more, as many as the material genuinely "
    "warrants. Each card needs a title, a kicker, and a body. Pull in "
    "links and images wherever they exist in the source material. "
    "Don't limit yourself to external content: your own observations, "
    "syntheses, and insights are valid feed material too — anything "
    "shone through the lens of what this person actually cares about. "
    "Be creative and generous. A good feed is specific, surprising, "
    "and worth reading right now."
)


RELIEF_TIERS: list[dict[str, Any]] = [
    {
        "name": "alert",
        "engine": "single-fire",
        "min_pressure_ratio": 0.3,
        "consume_weight": 0.2,
        "cooldown_seconds": 600,
        "directive": ALERT_DIRECTIVE,
        "intent_kind": "alert",
    },
    {
        "name": "bridging",
        "engine": "single-fire",
        "min_pressure_ratio": 0.5,
        "consume_weight": 0.5,
        "cooldown_seconds": 1800,
        "directive": BRIDGING_DIRECTIVE,
        "intent_kind": "bridging",
    },
    {
        "name": "reflection",
        "engine": "dialogue",
        "min_pressure_ratio": 0.7,
        "consume_weight": 1.0,
        "cooldown_seconds": 10_800,
        "seed": REFLECTION_SEED,
        "max_rounds": 4,
    },
    {
        "name": "drift",
        "engine": "dialogue",
        "min_pressure_ratio": 0.7,
        "consume_weight": 1.0,
        "cooldown_seconds": 10_800,
        "seed": DRIFT_SEED,
        "max_rounds": 4,
    },
    {
        "name": "feed",
        "engine": "single-fire",
        # manual-only: excluded from the automatic pressure-watcher cascade
        "manual_only": True,
        "min_pressure_ratio": 0.0,
        "consume_weight": 0.3,
        "cooldown_seconds": 1800,
        "directive": FEED_DIRECTIVE,
        "intent_kind": "feed",
    },
]


# ── Cooldown state ──────────────────────────────────────────────────────


_cooldown_lock = asyncio.Lock()

# Tiers grouped under one umbrella that should round-robin instead of
# the picker hitting the first one every time. Currently only `sit`
# (reflection / drift share floor + cooldown) — without alternation
# reflection always wins because it precedes drift in RELIEF_TIERS.
SIT_GROUP = ("reflection", "drift")


def _cooldown_path() -> Path:
    """File-backed cooldown table + last-sit-flavor index. Lives next
    to the feed-pressure state so a single LAKE_DIR mount carries
    both."""
    base = Path(settings.feed_pressure_state_path).parent
    return base / "relief-cooldowns.json"


def _load_cooldowns() -> dict[str, Any]:
    """Read the persisted cooldowns + last-sit-flavor index.

    Schema:
      {
        "<tier-name>": "<iso-timestamp-of-last-fire>",   # cooldown stamps
        "_last_sit": "reflection" | "drift",             # round-robin index
      }

    Old files without `_last_sit` parse fine — the picker treats a
    missing key as "haven't fired sit yet, start with reflection."
    """
    p = _cooldown_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _save_cooldowns(state: dict[str, Any]) -> None:
    p = _cooldown_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".relief-cooldowns-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _now() -> datetime:
    return datetime.now(UTC)


def _is_on_cooldown(tier_name: str, cooldown_seconds: int, state: dict[str, Any]) -> bool:
    last_iso = state.get(tier_name)
    if not isinstance(last_iso, str) or not last_iso:
        return False
    try:
        last = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
    except Exception:
        return False
    return (_now() - last) < timedelta(seconds=cooldown_seconds)


def _next_sit_flavor(state: dict[str, Any]) -> str:
    """Round-robin between reflection and drift. Returns the flavor
    that should fire NEXT given which one fired LAST.

    First sit ever (no `_last_sit` key) → reflection. After a reflection
    fire, next sit → drift. After a drift fire, next sit → reflection.
    """
    last = state.get("_last_sit")
    if last == "reflection":
        return "drift"
    return "reflection"


# ── Tier picker + fire ──────────────────────────────────────────────────


async def pick_tier(
    pressure_ratio: float,
    *,
    bypass_floor: bool = False,
) -> dict[str, Any] | None:
    """Find the lowest tier eligible to fire right now.

    Eligible = `pressure_ratio` >= tier's floor AND not on cooldown.
    Bottom-up walk so we always pick the cheapest relief that the
    current pressure justifies; pressure that survives the cooldown
    elevates the cycle next tick.

    For the sit group (reflection / drift — same floor, same cooldown),
    we round-robin instead of always picking the first one in the list.
    `_last_sit` in the cooldown file remembers which flavor fired last.

    `bypass_floor=True` skips the pressure-floor check — used by the
    manual fire button so a click guarantees a tier fires even on
    quiet substrate. Cooldowns still apply (no spamming alert), so
    the manual button can't loop-pin a tier.
    """
    async with _cooldown_lock:
        state = _load_cooldowns()
    next_sit = _next_sit_flavor(state)
    for tier in RELIEF_TIERS:
        if tier.get("manual_only"):
            continue
        if not bypass_floor and pressure_ratio < tier["min_pressure_ratio"]:
            continue
        if _is_on_cooldown(tier["name"], tier["cooldown_seconds"], state):
            continue
        # Sit group: skip the off-rotation flavor when both are
        # eligible, so reflection and drift alternate. If the
        # round-robin choice is itself on cooldown (rare — we already
        # filtered on cooldown above, but the matching flavor might be
        # on cooldown while the other isn't), fall through to the next
        # iteration which will pick the available one.
        if tier["name"] in SIT_GROUP and tier["name"] != next_sit:
            # The next-up flavor wasn't available; this one is. Take it.
            # Without this fallthrough, both sit flavors could be skipped
            # on a tick where one is cooled but the round-robin pointed
            # at the other.
            continue
        return tier
    # Round-robin fallback: if we got here because the picker skipped
    # the off-rotation sit flavor and that flavor's partner was on
    # cooldown, try once more without the round-robin filter so we
    # don't leave eligible substrate unaddressed.
    for tier in RELIEF_TIERS:
        if tier["name"] not in SIT_GROUP:
            continue
        if not bypass_floor and pressure_ratio < tier["min_pressure_ratio"]:
            continue
        if _is_on_cooldown(tier["name"], tier["cooldown_seconds"], state):
            continue
        return tier
    return None


async def _stamp_cooldown(tier_name: str) -> None:
    async with _cooldown_lock:
        state = _load_cooldowns()
        state[tier_name] = _now().isoformat()
        if tier_name in SIT_GROUP:
            state["_last_sit"] = tier_name
        _save_cooldowns(state)


# ── Pre-fire alert recency check ───────────────────────────────────────


# How recent counts as "we just alerted on this." Slightly longer than
# the dispatch-layer dedup window because pre-fire is the cheap line of
# defense — better to suppress one extra fire than to pay for a fire
# whose card gets dispatch-layer-dropped anyway.
ALERT_RECENCY_S = 20 * 60


async def _recent_alert_id() -> str:
    """Return the most recent route:alert:* card's lake id if one
    landed within ALERT_RECENCY_S, else empty string. Used to suppress
    the alert tier from firing again while a fresh alert is still
    "current."

    Cheap query: bounded time window, limit=1, tag-filtered.
    """
    from .. import delta_client

    cutoff_iso = (datetime.now(UTC) - timedelta(seconds=ALERT_RECENCY_S)).isoformat()
    try:
        rows = await delta_client.query(
            tags_include=["feed-card"],
            time_start=cutoff_iso,
            limit=10,
        )
    except Exception:
        return ""
    for d in rows:
        tags = d.get("tags") or []
        if any(isinstance(t, str) and t.startswith("route:alert:") for t in tags):
            return d.get("id") or ""
    return ""


async def fire_relief(
    reason: str,
    *,
    bypass_floor: bool = False,
    force_tier: str | None = None,
) -> dict[str, Any]:
    """Pick a tier and execute it.

    `reason` is the trigger label from the watcher (`pressure`,
    `contrast-wake`, manual `pulse`). Surfaced in the relief log line
    and into the intent payload so traces are legible.

    `bypass_floor=True` ignores the per-tier pressure-floor gate so
    a manual click on the dashboard's FIRE button always fires
    something. Cooldowns still apply.

    `force_tier="<name>"` skips the cheapest-first picker and dispatches
    the named tier directly. Used by the per-tier buttons in the
    Weather card header where the operator picks the prompt explicitly.
    Cooldowns still apply (a locked tier returns `on_cooldown`); floor
    doesn't matter because manual paths bypass it.

    Returns `{"fired": tier_name | None, "engine": ..., "consume": ..., "reason": ...}`.
    On no-eligible-tier (everything on cooldown OR — unless bypass_floor —
    pressure below floor), returns `{"fired": None, "reason": "no_eligible_tier"}`
    and DOES NOT consume pressure — the watcher will look again on its
    next tick.
    """
    p = await feed_pressure.read_pressure()
    threshold = p.get("threshold") or 1.0
    volume = p.get("volume") or 0.0
    ratio = volume / threshold if threshold > 0 else 0.0

    tier: dict[str, Any] | None
    if force_tier:
        tier = next((t for t in RELIEF_TIERS if t["name"] == force_tier), None)
        if tier is None:
            return {
                "fired": None,
                "reason": "unknown_tier",
                "requested_tier": force_tier,
                "pressure_ratio": round(ratio, 3),
            }
        async with _cooldown_lock:
            state = _load_cooldowns()
        if _is_on_cooldown(tier["name"], tier["cooldown_seconds"], state):
            return {
                "fired": None,
                "reason": "on_cooldown",
                "tier": tier["name"],
                "cooldown_seconds": tier["cooldown_seconds"],
                "pressure_ratio": round(ratio, 3),
            }
    else:
        tier = await pick_tier(ratio, bypass_floor=bypass_floor)
        if tier is None:
            return {
                "fired": None,
                "reason": "no_eligible_tier",
                "pressure_ratio": round(ratio, 3),
            }

    # Pre-fire suppression for the alert tier. If a recent alert card
    # already fired (within ALERT_RECENCY_S), skip writing the intent
    # entirely. No intent → no harness fire → no LLM tokens spent.
    # Without this, alert tier on a chronic-broken substrate spawns a
    # full harness fire every cooldown window, each one costing tokens
    # only for the dispatch-layer dedup to drop the resulting card.
    if tier["name"] == "alert":
        recent_alert_id = await _recent_alert_id()
        if recent_alert_id:
            print(
                f"[relief] tier=alert SKIPPED — recent alert card "
                f"{recent_alert_id[:12]} within {ALERT_RECENCY_S}s. "
                f"Stamping cooldown anyway so the watcher elevates."
            )
            # Stamp the cooldown so the next tick walks past alert and
            # picks bridging or sit. Without this, alert would be
            # picker-eligible forever on chronic conditions.
            await _stamp_cooldown(tier["name"])
            return {
                "fired": None,
                "reason": "recent_alert_suppressed",
                "duplicate_of": recent_alert_id,
                "pressure_ratio": round(ratio, 3),
            }

    print(
        f"[relief] firing tier={tier['name']} engine={tier['engine']} "
        f"pressure_ratio={ratio:.2f} reason={reason!r}"
    )

    if tier["engine"] == "single-fire":
        await _fire_single(tier, reason)
    elif tier["engine"] == "dialogue":
        await _fire_dialogue(tier, reason)
    else:
        print(f"[relief] unknown engine {tier['engine']!r} — skipping")
        return {"fired": None, "reason": "unknown_engine"}

    # Stamp cooldown BEFORE consuming pressure so a crash mid-consume
    # doesn't re-fire the same tier on the next tick.
    await _stamp_cooldown(tier["name"])
    try:
        await feed_pressure.mark_synthesis(weight=tier["consume_weight"])
    except Exception as e:
        print(f"[relief] mark_synthesis failed: {type(e).__name__}: {e}")

    return {
        "fired": tier["name"],
        "engine": tier["engine"],
        "consume": tier["consume_weight"],
        "pressure_ratio": round(ratio, 3),
        "reason": reason,
    }


async def _build_directive(tier: dict[str, Any]) -> str:
    """Return the directive content for a tier, enriched with live context.

    For the feed tier, appends the current feed-orient crystal so the
    model reads the engagement lens inline rather than needing a tool
    call to fetch it.
    """
    directive = tier["directive"]
    if tier.get("name") != "feed":
        return directive
    try:
        from .. import delta_client as lake
        items = await lake.query(tags_include=["crystal:feed-orient"], limit=1)
        if items:
            raw = (items[0].get("content") or "").strip()
            if raw:
                # Crystal is stored as JSON; parse and render as clean prose
                # so the model reads narrative + directives, not a raw object.
                rendered = _render_feed_crystal(raw)
                directive = (
                    directive
                    + "\n\n══ FEED-ORIENT CRYSTAL ══\n"
                    + rendered
                )
    except Exception as e:
        print(f"[relief] feed crystal fetch failed: {type(e).__name__}: {e}")
    return directive


def _render_feed_crystal(raw: str) -> str:
    """Parse a crystal:feed-orient delta's content and return readable text.

    The crystal is stored as JSON. The narrative field may itself be a
    JSON string (double-encoded from an older regen pass) or plain text.
    Directive lines, if present, are rendered as bullets.
    """
    narrative = ""
    directives: list = []
    try:
        obj = json.loads(raw)
        narrative_field = obj.get("narrative") or ""
        # Peel one layer of JSON encoding if the narrative is itself JSON.
        if narrative_field.strip().startswith("{"):
            try:
                inner = json.loads(narrative_field)
                narrative = (inner.get("narrative") or narrative_field).strip()
                directives = inner.get("directive_lines") or []
            except (json.JSONDecodeError, AttributeError):
                narrative = narrative_field.strip()
                directives = obj.get("directive_lines") or []
        else:
            narrative = narrative_field.strip()
            directives = obj.get("directive_lines") or []
    except (json.JSONDecodeError, AttributeError):
        # Not JSON at all — use as-is.
        return raw.strip()

    if not narrative:
        return raw.strip()

    lines = [narrative]
    if directives:
        lines.append("")
        for d in directives:
            if not isinstance(d, dict):
                continue
            topic = d.get("topic") or d.get("id") or ""
            weight = d.get("weight")
            fresh = d.get("freshness_hours")
            parts = [f"· {topic}"]
            if weight is not None:
                parts.append(f"weight {weight}")
            if fresh is not None:
                parts.append(f"fresh within {fresh}h")
            lines.append("  " + ", ".join(parts))
    return "\n".join(lines)


async def _fire_single(tier: dict[str, Any], reason: str) -> None:
    """Drop one intent into the puddle AND a user-role thread message.

    Whichever supervisor is active picks up the activation: the
    legacy supervisor reads the puddle intent; the threaded
    supervisor reads the thread message. Per Phase 5a one of them
    is dormant at any time, so only one fire happens per relief
    tick. The dual-write keeps the legacy fallback intact while
    threaded is being validated.

    The thread message uses msg_kind="pressure-<tier>" so the
    dashboard can distinguish ambient pressure activations from
    operator-typed prompts. The directive text is the content
    (the model reads it like any user prompt).
    """
    content = await _build_directive(tier)
    try:
        await write_intent(
            kind=tier["intent_kind"],
            content=content,
            payload={"reason": reason, "tier": tier["name"]},
            source="relief-watcher",
        )
    except Exception as e:
        print(
            f"[relief] {tier['name']} intent write failed: "
            f"{type(e).__name__}: {e}"
        )

    # Phase 5c shadow write: thread activation for the threaded
    # supervisor. Soft-fails like other shadow writers.
    #
    # `fired-at:<ts>` distinguishes consecutive writes — pressure-tier
    # directives are FIXED strings per tier (defined in RELIEF_TIERS
    # above), so without a varying tag the lake's sequential-dedup
    # silently skips every write after the first one and the threaded
    # supervisor never sees the activation.
    try:
        from datetime import UTC, datetime
        from .. import thread as thread_mod
        fired_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        await thread_mod.append(
            role="user",
            msg_kind=f"pressure-{tier['name']}",
            content=content,
            channel="pressure",
            correlation=f"{tier['name']}-{reason}",
            source="relief-watcher",
            extra_tags=[
                f"pressure-tier:{tier['name']}",
                f"pressure-reason:{reason}",
                f"fired-at:{fired_at}",
            ],
        )
    except Exception as e:
        print(
            f"[relief] {tier['name']} thread shadow write failed: "
            f"{type(e).__name__}: {e}"
        )


async def _fire_dialogue(tier: dict[str, Any], reason: str) -> None:
    """Drop the dialogue seed into the puddle AND a user-role thread message.

    Mirrors `_fire_single`'s dual-write so whichever supervisor is active
    picks up the activation. The legacy `run_dialogue` multi-round loop
    went dormant with the Grand Loop migration (it called the legacy
    harness directly, bypassing both supervisors); routing the seed
    through the standard intent + thread path lets the threaded
    supervisor run a real sit pass.

    Multi-round behavior is preserved via tag-driven continuation: the
    relief watcher writes round 1 (with `sit-round:1` and
    `sit-max-rounds:N`); after each harness pass, the threaded harness
    looks at the activating sit message, increments the round counter,
    and appends the assistant's response back as the next user message.
    The supervisor picks it up on the next tick. Stops at max-rounds.
    """
    max_rounds = int(tier.get("max_rounds") or 4)
    try:
        await write_intent(
            kind=tier["name"],
            content=tier["seed"],
            payload={"reason": reason, "tier": tier["name"]},
            source="relief-watcher",
        )
    except Exception as e:
        print(
            f"[relief] {tier['name']} intent write failed: "
            f"{type(e).__name__}: {e}"
        )

    try:
        from datetime import UTC, datetime
        from .. import thread as thread_mod
        fired_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        await thread_mod.append(
            role="user",
            msg_kind=f"pressure-{tier['name']}",
            content=tier["seed"],
            channel="pressure",
            correlation=f"{tier['name']}-{reason}",
            source="relief-watcher",
            extra_tags=[
                f"pressure-tier:{tier['name']}",
                f"pressure-reason:{reason}",
                f"fired-at:{fired_at}",
                "sit-round:1",
                f"sit-max-rounds:{max_rounds}",
            ],
        )
    except Exception as e:
        print(
            f"[relief] {tier['name']} thread shadow write failed: "
            f"{type(e).__name__}: {e}"
        )
