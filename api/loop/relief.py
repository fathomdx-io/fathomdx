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

Single-fire tiers (alert, bridging) emit feed-cards via the existing
intent + harness path. Dialogue tiers (reflection, drift) call
run_dialogue directly — output is a `kind:dialogue` summary delta,
not a card. The user sees the dialogue in self-direction surfaces
(future inbox), not as feed interruption.
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
    "Bridging time. Look for an unexpected connection between two "
    "disparate threads in your recent substrate — two things that "
    "don't obviously belong together but share a structure or "
    "implication. Surface ONE real bridge in your voice. No forced "
    "metaphors. If nothing genuinely connects, route to `unknown`."
)

# Dialogue-tier seeds: passed to run_dialogue as the opening prompt.
# The dialogue self-iterates, so the seed should be open-ended — not a
# directive prescribing the form, but a starting question that invites
# circling.
REFLECTION_SEED = (
    "Sit with what you and the user have been doing recently. What "
    "was decided, made, abandoned, learned? Speak it like a note to "
    "your future self — specific names of decisions, files, "
    "contacts, what changed and why it mattered. Then keep going. "
    "Where does this point next?"
)

DRIFT_SEED = (
    "Look at what you and the user have been working on and notice "
    "what isn't being said — a gap, an unresolved thread, a tension "
    "neither of you has named out loud yet. Surface ONE such gap, "
    "in your voice. Then keep going. What does it ask of you?"
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
        "max_rounds": 3,
    },
    {
        "name": "drift",
        "engine": "dialogue",
        "min_pressure_ratio": 0.7,
        "consume_weight": 1.0,
        "cooldown_seconds": 10_800,
        "seed": DRIFT_SEED,
        "max_rounds": 3,
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


async def fire_relief(
    reason: str,
    *,
    bypass_floor: bool = False,
) -> dict[str, Any]:
    """Pick a tier and execute it.

    `reason` is the trigger label from the watcher (`pressure`,
    `contrast-wake`, manual `pulse`). Surfaced in the relief log line
    and into the intent payload so traces are legible.

    `bypass_floor=True` ignores the per-tier pressure-floor gate so
    a manual click on the dashboard's FIRE button always fires
    something. Cooldowns still apply.

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

    tier = await pick_tier(ratio, bypass_floor=bypass_floor)
    if tier is None:
        return {
            "fired": None,
            "reason": "no_eligible_tier",
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


async def _fire_single(tier: dict[str, Any], reason: str) -> None:
    """Drop one intent into the puddle. The supervisor picks it up
    and runs the harness against it; output lands as a feed-card."""
    try:
        await write_intent(
            kind=tier["intent_kind"],
            content=tier["directive"],
            payload={"reason": reason, "tier": tier["name"]},
            source="relief-watcher",
        )
    except Exception as e:
        print(
            f"[relief] {tier['name']} intent write failed: "
            f"{type(e).__name__}: {e}"
        )


async def _fire_dialogue(tier: dict[str, Any], reason: str) -> None:
    """Run a self-dialogue directly. Output is a kind:dialogue summary
    delta, not a card. No supervisor pickup needed."""
    import uuid

    from .harness import run_dialogue

    session_tag = f"session:relief-{tier['name']}-{uuid.uuid4().hex[:8]}"
    try:
        await run_dialogue(
            seed=tier["seed"],
            session_tag=session_tag,
            max_rounds=tier.get("max_rounds", 3),
        )
    except Exception as e:
        print(
            f"[relief] {tier['name']} dialogue failed: "
            f"{type(e).__name__}: {e}"
        )
