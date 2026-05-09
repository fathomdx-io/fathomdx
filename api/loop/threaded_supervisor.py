"""Threaded harness supervisor.

Polls the global thread for unaddressed user-role messages. When any
are present (and we're not already firing), hands off to
`run_threaded_fire`. Counterpart to the legacy `worker.py` supervisor,
which polled the puddle's `pending_intents`.

The activation rule is the design we settled on: the rolling window
IS the queue. Anything in it that hasn't been marked addressed is
load-bearing; the harness fires until nothing pending remains, then
sleeps until a new user message lands.

Phase 3 ships this behind the `FATHOM_THREADED_HARNESS=1` env flag.
When unset (the default), this supervisor never starts; the legacy
worker continues to drive the production loop. Flip the flag, restart
the api, and the threaded path takes over.

Concurrency safety: only one fire runs at a time. New user messages
arriving during a fire are handled on the next poll — the in-flight
fire reads the window snapshot it loaded; the next fire picks up the
new message.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from .. import thread as thread_mod
from .harness.threaded import run_threaded_fire

# How often to check for unaddressed messages when idle. Short enough
# that a freshly-landed user message fires within a couple seconds;
# long enough that an idle install doesn't burn CPU on lake queries.
IDLE_POLL_S = 1.5

# How long to wait between fires when there's still pending work
# (i.e. the previous fire didn't drain the queue). Short — the loop
# is genuinely chasing pending intents at this point.
BUSY_GAP_S = 0.2

# Hard cap on consecutive fires per pending burst. Without this, a
# pathological "model never marks anything addressed" pattern would
# spin forever on the same queue. After this many fires in a row
# without queue shrinkage, idle out and let the next user message
# (or operator inspection) trigger a fresh investigation.
MAX_CONSECUTIVE_FIRES = 5


_supervisor_task: asyncio.Task | None = None
_fire_lock = asyncio.Lock()

# Tracks the in-flight `run_threaded_fire` task so the operator stop
# button (POST /v1/loop/stop → cancel_active_fire) can interrupt a
# runaway loop. Set when a fire starts, cleared when it returns.
_active_fire_task: asyncio.Task | None = None


def cancel_active_fire() -> bool:
    """Cancel the in-flight harness fire, if any. Returns True if a fire
    was running and got cancelled.

    The supervisor loop catches the resulting CancelledError on the
    fire task (without exiting itself) and continues to the next tick.
    Pairing this with a queue-clear (see routes.stop_loop) keeps the
    supervisor from immediately re-firing on the same pending messages.
    """
    task = _active_fire_task
    if task is None or task.done():
        return False
    task.cancel()
    return True


def is_enabled() -> bool:
    """True when FATHOM_THREADED_HARNESS=1 in the environment.

    Read fresh each call so a settings reload (or test) takes effect
    immediately without restarting the supervisor.
    """
    return os.environ.get("FATHOM_THREADED_HARNESS", "0") == "1"


async def _supervisor_loop() -> None:
    """One forever-loop: poll, fire if pending, sleep, repeat."""
    global _active_fire_task
    print(f"[threaded-supervisor armed] flag=on poll={IDLE_POLL_S:.1f}s")
    consecutive_fires = 0
    last_pending_count = -1
    while True:
        try:
            await asyncio.sleep(IDLE_POLL_S if consecutive_fires == 0 else BUSY_GAP_S)
        except asyncio.CancelledError:
            return

        try:
            window = await thread_mod.build_window()
        except Exception as e:
            print(f"[threaded-supervisor] window read crashed: {type(e).__name__}: {e}")
            consecutive_fires = 0
            continue

        pending = window["unaddressed"]
        pending_count = len(pending)

        if pending_count == 0:
            consecutive_fires = 0
            last_pending_count = -1
            continue

        # Anti-spin: if the queue isn't shrinking across consecutive
        # fires, the model is failing to address what's in front of it.
        # Bail out to idle and let the next external trigger reset us.
        if (
            consecutive_fires >= MAX_CONSECUTIVE_FIRES
            and pending_count >= last_pending_count
            and last_pending_count > 0
        ):
            print(
                f"[threaded-supervisor] anti-spin: {consecutive_fires} fires "
                f"without progress (pending={pending_count}); idling"
            )
            consecutive_fires = 0
            last_pending_count = -1
            await asyncio.sleep(IDLE_POLL_S * 4)
            continue

        last_pending_count = pending_count
        consecutive_fires += 1

        async with _fire_lock:
            fire_task = asyncio.create_task(run_threaded_fire(), name="loop/threaded-fire")
            _active_fire_task = fire_task
            try:
                result = await fire_task
            except asyncio.CancelledError:
                # Distinguish operator-driven fire cancel from supervisor
                # shutdown. If the OUTER supervisor task is being
                # cancelled (container teardown), `cancelling()` is > 0
                # — propagate. Otherwise the cancel came from
                # `cancel_active_fire()` (the stop button); eat it and
                # let the next tick re-evaluate.
                outer = asyncio.current_task()
                if outer is not None and outer.cancelling() > 0:
                    return
                print("[threaded-supervisor] fire cancelled by operator")
                consecutive_fires = 0
                last_pending_count = -1
                continue
            except Exception as e:
                print(f"[threaded-supervisor] fire crashed: {type(e).__name__}: {e}")
                continue
            finally:
                _active_fire_task = None
        addressed = result.get("addressed") or []
        body_chars = len((result.get("final_response") or {}).get("body") or "")
        print(
            f"[threaded-supervisor] fire complete: turns={result.get('turns')} "
            f"addressed={len(addressed)} body[{body_chars}c] "
            f"lake-id={(result.get('lake_id') or 'none')[:12]}"
        )


def start() -> None:
    """Start the supervisor task. Idempotent. No-op when the flag is off."""
    global _supervisor_task
    if not is_enabled():
        print("[threaded-supervisor] FATHOM_THREADED_HARNESS not set — supervisor idle")
        return
    if _supervisor_task and not _supervisor_task.done():
        return
    _supervisor_task = asyncio.create_task(
        _supervisor_loop(),
        name="loop/threaded-supervisor",
    )


async def stop() -> None:
    """Cancel the supervisor task. Awaits clean shutdown."""
    global _supervisor_task
    if _supervisor_task is None:
        return
    _supervisor_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _supervisor_task
    _supervisor_task = None
