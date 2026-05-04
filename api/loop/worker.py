"""Grand Loop supervisor.

Polls the puddle for pending intents. When any are present, runs an
intent-searcher pre-pass to seed substrate, then hands the fire to the
agentic harness — the model elects its own deliberation via tool
calls, produces a response, and dispatches the card.

The convener+parliament+witness path was retired in the harness
migration; the harness's `deliberate` tool covers the antagonism case
when needed.
"""

from __future__ import annotations

import asyncio
import uuid

from .. import standpoint as standpoint_mod
from . import feed_orient
from .claude_code_watcher import claude_code_watcher_loop
from .harness import run_harness
from .intents import next_intent_group, pending_intents
from .pressure import pressure_watcher
from .puddle import puddle
from .recall import run_intent_searcher_tick
from .telepathy import telepathy_loop


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
_claude_code_task: asyncio.Task | None = None
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
    """Run one harness fire against the next pending intent group.

    Returns True if a fire happened (work was done). The caller idles
    when there was nothing pending.
    """
    all_pending = pending_intents(since_iso=_boot_iso)
    if not all_pending:
        return False
    # Fire one (channel, correlation) group at a time. Two concurrent
    # OpenAI sessions become two sequential fires, not one collapsed
    # card. Channel-less ambient intents still batch as today.
    pending = next_intent_group(all_pending)
    if not pending:
        return False

    session_tag = f"session:{uuid.uuid4().hex[:12]}"
    print(
        f"[loop fire] {session_tag} pending={len(pending)} "
        f"(of {len(all_pending)} total across groups)"
    )

    # Standpoint — gather Fathom's self-state ONCE at fire start. The
    # harness reads from this consistent snapshot rather than re-fetching
    # mid-fire and risking a torn read. Soft-fails: any sub-loader
    # exception inside standpoint.current() yields an empty component,
    # never propagates.
    try:
        standpoint = await standpoint_mod.current(session_tag=session_tag)
        print(
            f"[loop fire] standpoint: posture={standpoint.posture} "
            f"affect={standpoint.affect.state} "
            f"endorsements={len(standpoint.endorsements)} "
            f"understanding={len(standpoint.understanding)}"
        )
    except Exception as e:
        print(f"[loop fire] standpoint gather crashed: {type(e).__name__}: {e}")
        standpoint = None

    # Intent-seed pre-pass — seeds the puddle with one shallow recall-
    # result anchored on the user's literal intent. Gives the harness's
    # first turn substrate to lean on without needing a `semantic` call
    # for casual drop-ins. Soft-fails — first turn just sees less.
    try:
        await run_intent_searcher_tick(
            session_tag=session_tag,
            event_id=f"{session_tag.split(':', 1)[1]}-intent-seed",
            intents=pending,
        )
    except Exception as e:
        print(f"[loop fire] intent-searcher seed crashed: {type(e).__name__}: {e}")

    try:
        await run_harness(
            session_tag=session_tag,
            pending=pending,
            standpoint=standpoint,
        )
    except Exception as e:
        print(f"[loop fire] harness crashed: {type(e).__name__}: {e}")
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
    claude-code-watcher + feed-orient regen. Idempotent."""
    global _supervisor_task, _reaper_task, _telepathy_task, _pressure_task
    global _claude_code_task, _boot_iso
    if _supervisor_task is not None:
        return
    _boot_iso = _now_iso()
    _supervisor_task = asyncio.create_task(_supervisor(), name="loop/supervisor")
    _reaper_task = asyncio.create_task(_reaper(), name="loop/reaper")
    _telepathy_task = asyncio.create_task(telepathy_loop(), name="loop/telepathy")
    _pressure_task = asyncio.create_task(pressure_watcher(), name="loop/pressure")
    _claude_code_task = asyncio.create_task(
        claude_code_watcher_loop(), name="loop/claude-code-watcher"
    )
    feed_orient.start()


async def stop() -> None:
    """Cancel all background tasks. Idempotent."""
    global _supervisor_task, _reaper_task, _telepathy_task, _pressure_task
    global _claude_code_task
    await feed_orient.stop()
    for task in (
        _supervisor_task,
        _reaper_task,
        _telepathy_task,
        _pressure_task,
        _claude_code_task,
    ):
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
    _claude_code_task = None
