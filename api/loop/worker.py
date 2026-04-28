"""Grand Loop supervisor — minimum viable.

Polls the puddle for pending intents. When any are present, runs a
short parliament round (one tick per voice in rotation) then fires the
witness pass to produce one routed card. Addressed intents leave the
queue automatically because the witness output carries
`addresses:<intent-id>` tags that `pending_intents()` filters on.

This is the v1 spike — no ambient watcher, no pressure pulses, no
telepathy. Those wire in as separate background tasks in subsequent
commits without changing this loop's shape.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from . import feed_orient
from .intents import pending_intents
from .metric import (
    SETTLE_WINDOW,
    emit_metric,
    measure_cross_voice_convergence,
    settle_window_check,
)
from .pressure import pressure_watcher
from .process import run_process
from .prompts import VOICES
from .puddle import puddle
from .recall import run_searcher_tick
from .telepathy import telepathy_loop
from .witness import run_witness


# Maximum parliament rounds per fire. With settle detection in place
# the loop usually exits earlier — voices converge in 2-4 rounds when
# they're going to converge at all, and the witness fires the moment
# the rolling cross-voice spread tightens below SETTLE_SPREAD_MAX.
# MAX_ROUNDS is a safety cap so a deadlocked parliament can't run
# forever; the witness still fires after it (with a "did not settle"
# descriptor in the prompt) so an unresolved tension gets named honestly.
MAX_ROUNDS_PER_FIRE = 8

# Idle sleep — when there's nothing pending, how long to wait before
# polling again. Short enough that a freshly-seeded intent fires within
# a few seconds; long enough that an idle install doesn't burn CPU.
IDLE_SLEEP_S = 1.5

# Reap interval — drop expired puddle entries. Queries already filter
# by expires_at so unreaped corpses don't leak into results; reap is a
# memory-pressure measure.
REAP_INTERVAL_S = 30


_supervisor_task: asyncio.Task | None = None
_reaper_task: asyncio.Task | None = None
_telepathy_task: asyncio.Task | None = None
_pressure_task: asyncio.Task | None = None
_boot_iso: str = ""


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


async def _reaper() -> None:
    """Background task — periodic puddle.reap()."""
    while True:
        try:
            await asyncio.sleep(REAP_INTERVAL_S)
            n = await puddle.reap()
            if n:
                print(f"[loop reap] dropped {n} expired delta(s)")
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[loop reap] crashed: {type(e).__name__}: {e}")


async def _run_one_fire() -> bool:
    """Run one full deliberation: parliament round(s) → witness.

    Returns True if a fire happened (work was done). The caller idles
    when there was nothing pending.
    """
    pending = pending_intents(since_iso=_boot_iso)
    if not pending:
        return False

    session_tag = f"session:{uuid.uuid4().hex[:12]}"
    print(f"[loop fire] {session_tag} pending={len(pending)}")

    convergence_samples: list[float] = []
    settled = False
    settle_level: float | None = None
    rounds_run = 0

    for round_idx in range(MAX_ROUNDS_PER_FIRE):
        rounds_run = round_idx + 1
        # Fire all voices + recall in parallel for this round. Each
        # voice reads the puddle as it stands at round-start (the prior
        # round's takes plus any telepathy mirrors), composes its
        # thought, and writes back. Recall reads the latest pre-round
        # voice thought (or the seed on round 0) and pulls hits in
        # parallel — so by the time witness fires, the substrate has
        # been enriched by both voices and recall together.
        voice_coros = [
            run_process(
                pid=f"{round_idx}-{v['name']}-{uuid.uuid4().hex[:6]}",
                session_tag=session_tag,
                voice=v,
                pending=pending,
            )
            for v in VOICES
        ]
        recall_coro = run_searcher_tick(
            session_tag=session_tag,
            event_id=f"{session_tag.split(':', 1)[1]}-r{round_idx}",
        )
        results = await asyncio.gather(*voice_coros, recall_coro, return_exceptions=True)

        # Map results back to voices for the convergence sample. Recall
        # is the trailing element (None on success, ignored otherwise).
        voice_thoughts: list[tuple[str, str]] = []
        for v, res in zip(VOICES, results[:-1]):
            if isinstance(res, Exception):
                print(f"[loop fire] {v['name']} crashed: {type(res).__name__}: {res}")
                continue
            voice_thoughts.append((v["name"], res or ""))
        recall_res = results[-1]
        if isinstance(recall_res, Exception):
            print(f"[loop fire] searcher crashed: {type(recall_res).__name__}: {recall_res}")

        # Per-voice convergence sample — measured against the OTHER
        # voices' takes (now that all three are written for this round
        # the comparison set is complete). Append samples in order so
        # the rolling window's spread reflects this round's spread.
        for voice_name, text in voice_thoughts:
            d = measure_cross_voice_convergence(
                text=text,
                voice_name=voice_name,
                session_tag=session_tag,
            )
            if d is None:
                continue
            convergence_samples.append(d)
            try:
                await emit_metric(
                    session_tag=session_tag,
                    voice_name=voice_name,
                    distance=d,
                )
            except Exception as e:
                print(f"[metric] emit crashed: {type(e).__name__}: {e}")

        # Settle check — exits the deliberation loop when the last
        # SETTLE_WINDOW samples span less than SETTLE_SPREAD_MAX.
        ok, level = settle_window_check(convergence_samples)
        if ok:
            settled = True
            settle_level = level
            break

    if settled:
        print(f"[loop fire] settled after {rounds_run} round(s) at level {settle_level:.2f}")
    else:
        print(f"[loop fire] did NOT settle after {rounds_run} round(s) (cap)")

    try:
        await run_witness(session_tag=session_tag, pending=pending)
    except Exception as e:
        print(f"[loop fire] witness crashed: {type(e).__name__}: {e}")
    return True


async def _supervisor() -> None:
    """Main loop — fire when pending, idle otherwise."""
    print(f"[loop] supervisor started boot_iso={_boot_iso}")
    while True:
        try:
            ran = await _run_one_fire()
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[loop tick] crashed: {type(e).__name__}: {e}")
            ran = False
        if not ran:
            try:
                await asyncio.sleep(IDLE_SLEEP_S)
            except asyncio.CancelledError:
                return


def start() -> None:
    """Start supervisor + reaper + telepathy + pressure-watcher +
    feed-orient regen. Idempotent."""
    global _supervisor_task, _reaper_task, _telepathy_task, _pressure_task, _boot_iso
    if _supervisor_task is not None:
        return
    _boot_iso = _now_iso()
    _supervisor_task = asyncio.create_task(_supervisor(), name="loop/supervisor")
    _reaper_task = asyncio.create_task(_reaper(), name="loop/reaper")
    _telepathy_task = asyncio.create_task(telepathy_loop(), name="loop/telepathy")
    _pressure_task = asyncio.create_task(pressure_watcher(), name="loop/pressure")
    feed_orient.start()


async def stop() -> None:
    """Cancel all background tasks. Idempotent."""
    global _supervisor_task, _reaper_task, _telepathy_task, _pressure_task
    await feed_orient.stop()
    for task in (_supervisor_task, _reaper_task, _telepathy_task, _pressure_task):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _supervisor_task = None
    _reaper_task = None
    _telepathy_task = None
    _pressure_task = None
