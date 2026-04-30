"""Background poller that fires routines on their cron schedule.

Walks every enabled routine spec each tick. For each one, computes
`next_fire_after(cron, pivot)` where pivot is the routine's last-fire
timestamp (or the scheduler boot time, whichever is later). If that
next-fire moment has already passed, write a routine-fire delta — the
fathom-agent kitty plugin picks it up from there.

`single_fire: true` routines are soft-deleted (tombstone delta) right
after they fire, so the next tick won't see them in the spec list.

Started/stopped by the FastAPI lifespan in api/server.py. Disable by
setting FATHOM_routine_scheduler_enabled=false.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from . import routines as routines_mod
from .settings import settings

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_boot_time: float = 0.0
# routine_id -> epoch of the last fire we wrote this process. Survives
# crashes via the lake (the next boot reads fire deltas to repopulate),
# but in-memory tracking avoids re-querying the lake every tick.
_last_fire_at: dict[str, float] = {}


def _pivot_for(routine_id: str) -> float:
    """Anchor for the next-fire calculation.

    Use whichever is later: the last fire we know about, or boot_time
    minus a small grace window. The grace lets a routine whose cron
    moment is just-before-boot still fire on the first tick instead of
    waiting a full cycle.
    """
    last = _last_fire_at.get(routine_id, 0.0)
    return max(last, _boot_time - 60.0)


async def _hydrate_last_fires() -> None:
    """Populate _last_fire_at from the lake on startup.

    Without this, a process restart would re-fire any routine whose
    next-cron-after-boot has already passed (e.g. a daily 10:10 routine
    that already fired today would fire again on a 10:30 restart).
    """
    try:
        fires = await routines_mod._fire_deltas()
    except Exception as e:
        print(
            f"[routine-scheduler] lake unreachable on hydrate "
            f"({type(e).__name__}: {e}), starting cold",
            flush=True,
        )
        return
    for d in fires:
        rid = routines_mod._routine_id_from_tags(d.get("tags") or [])
        if not rid:
            continue
        ts_epoch = routines_mod._ts_to_epoch(d.get("timestamp"))
        if ts_epoch > _last_fire_at.get(rid, 0.0):
            _last_fire_at[rid] = float(ts_epoch)


async def _check_once() -> None:
    """One scheduler pass — fire any routine whose next-cron has elapsed."""
    try:
        specs = await routines_mod._spec_deltas()
    except Exception as e:
        print(
            f"[routine-scheduler] lake unreachable, skipping tick "
            f"({type(e).__name__}: {e})",
            flush=True,
        )
        return

    # Latest spec per routine-id (matches list_routines logic).
    latest_spec: dict[str, dict] = {}
    for d in specs:
        rid = routines_mod._routine_id_from_tags(d.get("tags") or [])
        if not rid:
            continue
        prev = latest_spec.get(rid)
        if prev is None or d.get("timestamp", "") > prev.get("timestamp", ""):
            latest_spec[rid] = d

    now = time.time()
    for rid, d in latest_spec.items():
        meta, _body = routines_mod.parse_frontmatter(d.get("content", ""))
        if meta.get("deleted"):
            continue
        if not meta.get("enabled", True):
            continue
        schedule = (meta.get("schedule") or "").strip()
        if not schedule:
            continue

        pivot = _pivot_for(rid)
        next_fire = routines_mod.next_fire_after(schedule, pivot)
        if next_fire is None or next_fire > now:
            continue

        try:
            print(
                f"[routine-scheduler] firing {rid} (cron={schedule!r}, "
                f"last_fire={_last_fire_at.get(rid, 0.0):.0f}, "
                f"next={next_fire:.0f})",
                flush=True,
            )
            await routines_mod.fire(rid)
            _last_fire_at[rid] = now
        except Exception as e:
            print(
                f"[routine-scheduler] fire failed for {rid}: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )
            continue

        if meta.get("single_fire"):
            try:
                await routines_mod.soft_delete(rid)
                print(
                    f"[routine-scheduler] single_fire tombstoned {rid}",
                    flush=True,
                )
            except Exception as e:
                print(
                    f"[routine-scheduler] single_fire tombstone failed "
                    f"for {rid}: {type(e).__name__}: {e}",
                    flush=True,
                )


async def _loop() -> None:
    print(
        f"[routine-scheduler] loop starting "
        f"(poll={settings.routine_scheduler_poll_seconds}s)",
        flush=True,
    )
    assert _stop_event is not None
    await _hydrate_last_fires()
    while not _stop_event.is_set():
        try:
            await _check_once()
        except Exception as e:
            print(
                f"[routine-scheduler] poll error: {type(e).__name__}: {e}",
                flush=True,
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                _stop_event.wait(),
                timeout=settings.routine_scheduler_poll_seconds,
            )
    print("[routine-scheduler] loop stopped", flush=True)


def start() -> None:
    """Kick off the scheduler. Idempotent."""
    global _task, _stop_event, _boot_time
    if not settings.routine_scheduler_enabled:
        print("[routine-scheduler] disabled by settings", flush=True)
        return
    if _task is not None and not _task.done():
        return
    _boot_time = time.time()
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_loop(), name="routine-scheduler")


async def stop() -> None:
    """Signal the loop to exit. Awaits the task briefly."""
    global _task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except TimeoutError:
            _task.cancel()
        except Exception:
            pass
    _task = None
    _stop_event = None
