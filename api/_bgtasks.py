"""Fire-and-forget task spawning with safe reference-holding and logging.

The asyncio event loop keeps only a weak reference to tasks created via
`asyncio.create_task`. If the caller discards the returned task (a common
fire-and-forget idiom), the task can be garbage collected mid-flight and
its coroutine silently stops running — the bug RUF006 is flagging.

The standard mitigation is to add the task to a module-level set and drop
it on completion. Going one step further here: log any exception the task
raises, since a discarded task's `exception()` is never read otherwise —
another silent-failure mode.

Usage:

    from ._bgtasks import spawn

    spawn(some_coro(), name="crystal-regen")
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Module-level strong references. Adding prevents GC while the task runs;
# the done_callback drops the reference and logs any exception.
_background_tasks: set[asyncio.Task] = set()


def _on_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "background task %r failed",
            task.get_name(),
            exc_info=exc,
        )


def spawn(coro, *, name: str | None = None) -> asyncio.Task:
    """Schedule a coroutine as a fire-and-forget background task.

    Returns the task so callers who want to cancel later can still hold
    a reference themselves. The helper keeps its own reference regardless,
    so the common discard-the-return-value pattern is safe.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_on_done)
    return task
