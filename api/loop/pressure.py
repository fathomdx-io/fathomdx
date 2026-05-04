"""Pressure watcher — substrate-driven Grand Loop trigger.

Polls feed-pressure on a slow clock; on threshold crossing, delegates
to `relief.fire_relief` to pick a tier (alert / bridging / reflection /
drift) and consume some pressure. The tier table and per-tier
cooldowns live in `relief.py`; this module is just the trigger loop.

Old `fire_pressure_pulse` (four-intents-per-trigger shape) was retired
when the relief gradient landed. See `relief.py:RELIEF_TIERS` for
where the directives went.
"""

from __future__ import annotations

import asyncio

from .. import feed_pressure


# How often to check pressure. The expensive part is delta_client.pressure_volume;
# 60s matches the experiment's PRESSURE_POLL_INTERVAL_S — slow enough not
# to load the lake, fast enough that "pressure built up over the last
# stretch" lands within a minute.
PRESSURE_POLL_S = 60


async def pressure_watcher() -> None:
    """Background task — every PRESSURE_POLL_S, check feed-pressure.
    When it crosses threshold (or contrast-wake fires), delegate to
    `relief.fire_relief` to pick a tier and consume some pressure.

    The tiered shape replaces the old four-passes-at-once pulse: each
    tick fires AT MOST ONE tier (alert / bridging / reflection / drift),
    and pressure that survives a small bite naturally elevates to a
    deeper tier on subsequent ticks (alert cooldown ~10 min, sit ~3 h).
    """
    from . import relief

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
                await relief.fire_relief(reason)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[relief] fire crashed: {type(e).__name__}: {e}")
