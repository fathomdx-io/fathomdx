"""Grand Loop supervisor — minimum viable.

Polls the puddle for pending intents. When any are present, runs a
short parliament round (one tick per voice in rotation) then fires the
witness pass to produce one routed card. Addressed intents leave the
queue automatically because the witness output carries
`addresses:<intent-id>` tags that `pending_intents()` filters on.

This is the v1 spike — no ambient watcher, no pressure pulses, no
vampire tap. Those wire in as separate background tasks in subsequent
commits without changing this loop's shape.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from .intents import pending_intents
from .process import run_process
from .prompts import VOICES
from .puddle import puddle
from .vampire import vampire_loop
from .witness import run_witness


# Tick budget — how many voice rounds (one tick per voice) before
# witness fires. v1 uses one full rotation (3 ticks) so the parliament
# touches the question once each before integrating. Tunable; the
# experiment runs many more rotations with settle detection deciding
# when to stop.
ROUNDS_PER_FIRE = 1

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
_vampire_task: asyncio.Task | None = None
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

    for round_idx in range(ROUNDS_PER_FIRE):
        for voice in VOICES:
            pid = f"{round_idx}-{voice['name']}-{uuid.uuid4().hex[:6]}"
            try:
                await run_process(
                    pid=pid,
                    session_tag=session_tag,
                    voice=voice,
                    pending=pending,
                )
            except Exception as e:
                print(f"[loop fire] process {pid} crashed: {type(e).__name__}: {e}")

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
    """Start the supervisor + reaper + vampire-tap. Idempotent."""
    global _supervisor_task, _reaper_task, _vampire_task, _boot_iso
    if _supervisor_task is not None:
        return
    _boot_iso = _now_iso()
    _supervisor_task = asyncio.create_task(_supervisor(), name="loop/supervisor")
    _reaper_task = asyncio.create_task(_reaper(), name="loop/reaper")
    _vampire_task = asyncio.create_task(vampire_loop(), name="loop/vampire")


async def stop() -> None:
    """Cancel all background tasks. Idempotent."""
    global _supervisor_task, _reaper_task, _vampire_task
    for task in (_supervisor_task, _reaper_task, _vampire_task):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _supervisor_task = None
    _reaper_task = None
    _vampire_task = None
