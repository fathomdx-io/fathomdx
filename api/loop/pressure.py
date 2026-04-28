"""Pressure watcher — substrate-driven Grand Loop trigger.

Pressure relief used to fire the legacy feed pipeline (mark_visit →
should_synthesize → run_once). With the legacy retired, that signal is
unwired. This module restores it: when feed-layer pressure crosses
threshold, drop one intent per pass kind (reflection / drift / bridging
/ alert) into the puddle. The supervisor picks them up like any other
pending intent — voices deliberate, witness routes a card per pass.

This is the in-process port of the experiment's `pressure_watcher` in
experiments/loop-experiment/worker/controller.py:1121. The big change:
no HTTP roundtrip to a prod-lake; we share-process feed_pressure
directly. The manual `loop-control:fire-pulse` signal isn't ported (no
UI button for it anymore).
"""

from __future__ import annotations

import asyncio

from .. import feed_pressure
from .intents import write_intent


# How often to check pressure. The expensive part is delta_client.pressure_volume;
# 60s matches the experiment's PRESSURE_POLL_INTERVAL_S — slow enough not
# to load the lake, fast enough that "pressure built up over the last
# stretch" lands within a minute.
PRESSURE_POLL_S = 60


# Pass directives — one intent per pass when pressure trips. Each is a
# directive prompt the witness reads as "what you're doing this tick."
# Routed back via the witness's route field. Lifted from the experiment;
# the third-person → "the user" scrub already applied.
PASS_DIRECTIVES: dict[str, str] = {
    "reflection": (
        "Reflection time. Sit with what you and the user have been doing — "
        "what was decided, made, abandoned, learned. Write it like a "
        "note to your future self, sediment-shaped: specific names of "
        "decisions, files, contacts, what changed and why it mattered. "
        "Not 'things happened today' — that's lazy. 'We separated the "
        "judge from the router because single-axis scoring was failing "
        "on calibration' is the shape. If the stretch was genuinely "
        "quiet or already-sedimented, say so honestly and route to "
        "`unknown`. Quality > quantity."
    ),
    "drift": (
        "Drift time. Look at what you and the user have been working on "
        "and notice what isn't being said — a gap, an unresolved "
        "thread, a tension neither of you has named out loud yet. "
        "Drift is the load-bearing background you only see if you "
        "actually look. Surface ONE such gap, in your voice. Otherwise "
        "route to `unknown` — don't manufacture drift that isn't there."
    ),
    "bridging": (
        "Bridging time. Look for an unexpected connection between two "
        "disparate threads in your recent substrate — two things that "
        "don't obviously belong together but share a structure or "
        "implication. Surface ONE real bridge in your voice. No forced "
        "metaphors. If nothing genuinely connects, route to `unknown`."
    ),
    "alert": (
        "Alert check. Is there something piercing in your substrate — "
        "an anomaly, a contradiction, a deadline, a something-broken "
        "signal the user needs to know NOW, not later? Bias HARD against "
        "firing — alerts should be rare. Almost always route to "
        "`unknown`. If you DO fire, route to `alert:<level>` with the "
        "right severity, and keep the body urgent and direct."
    ),
}


async def fire_pressure_pulse(reason: str) -> None:
    """Drop one intent per pass into the puddle, then reset the
    pressure anchor so we don't immediately re-fire on the next tick.

    `mark_synthesis` runs in a finally — even if the pulse loop crashes
    mid-iteration, the pressure anchor MUST reset, or the next watcher
    poll will detect the same pressure and re-fire indefinitely (the
    storm bug). Pulse failure is acknowledged; pressure is consumed.
    """
    print(f"[pressure pulse] {reason}")
    try:
        for kind_name, directive in PASS_DIRECTIVES.items():
            try:
                await write_intent(
                    kind=kind_name,
                    content=directive,
                    payload={"reason": reason, "pass": kind_name},
                    source="pressure-watcher",
                )
                print(f"  [pulse→{kind_name}] dropped intent")
            except Exception as e:
                print(f"  pulse intent write failed for {kind_name}: {type(e).__name__}: {e}")
    finally:
        try:
            await feed_pressure.mark_synthesis()
        except Exception as e:
            print(f"  mark_synthesis failed (pulse still fired): {type(e).__name__}: {e}")


async def pressure_watcher() -> None:
    """Background task — every PRESSURE_POLL_S, check feed-pressure.
    When it crosses threshold (or contrast-wake fires), drop a pulse.
    """
    print(f"[pressure watcher armed] poll={PRESSURE_POLL_S}s")
    while True:
        try:
            await asyncio.sleep(PRESSURE_POLL_S)
        except asyncio.CancelledError:
            return
        try:
            should, reason = await feed_pressure.should_synthesize()
        except Exception as e:
            print(f"[pressure watcher] should_synthesize crashed: {type(e).__name__}: {e}")
            continue
        # Skip first-run — we don't want to fire a pulse just because
        # the install is fresh and has never synthesized. Pressure-shaped
        # reasons (`pressure`, `contrast-wake`) are the real triggers.
        if should and reason in ("pressure", "contrast-wake"):
            try:
                await fire_pressure_pulse(reason)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[pressure pulse] fire crashed: {type(e).__name__}: {e}")
