"""Background loop wiring.

Boots the harness supervisor (`threaded_supervisor`), the puddle
reaper, the pressure watcher, the claude-code watcher, and the
feed-orient regen on startup; tears them down on shutdown. Also runs
a cold-start `rehydrate_puddle()` task that seeds the puddle with the
last few hours of conversation deltas so the first post-restart fire
has substrate context.

The harness itself lives in `api/loop/harness/threaded.py`; the
supervisor that drives it lives in `api/loop/threaded_supervisor.py`.
This module is purely the lifecycle / boot wiring.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from .. import delta_client
from . import feed_orient
from .claude_code_watcher import claude_code_watcher_loop
from .intents import CONVO_TAG
from .pressure import pressure_watcher
from .puddle import puddle

# Reap interval — drop expired puddle entries. Queries already filter
# by expires_at so unreaped corpses don't leak into results; reap is a
# memory-pressure measure.
REAP_INTERVAL_S = 30


_reaper_task: asyncio.Task | None = None
_pressure_task: asyncio.Task | None = None
_helper_task: asyncio.Task | None = None
_boot_iso: str = ""


def _now_iso() -> str:
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


REHYDRATE_WINDOW_HOURS = 6
REHYDRATE_MAX_TURNS = 30


async def rehydrate_puddle() -> None:
    """Cold-start: seed the puddle with recent conversation turns from
    the lake so the first post-restart fire has substrate context.

    The puddle is the attention space — whatever lands here, the loop
    pays attention to. So this pulls only the surfaces where Fathom is
    a participant in the conversation, not a bystander observing other
    chatter:
      · Q — composer seeds and openai-compat user turns (both shapes
            land as `kind:question`; that single slice covers them)
      · A — witness output (`feed-card`) covers chat-reply, alerts,
            claude-code dispatches, tool proposals — all of Fathom's
            authored turns regardless of route
      · claude-code task closures the watcher minted as intents
      · routine machinery (cron fires + summaries + due markers)

    Deliberately NOT included:
      · `assistant`-tagged deltas — in practice this catches free-
        floating claude-code chat (operator talking with their coding
        assistant on a session NOT dispatched by Fathom). The puddle
        isn't a lake mirror; if Fathom hasn't been addressed, those
        turns don't belong in its working memory.
      · `participant:user` — guess that didn't match how openai-compat
        actually tags (user turns there are `user-seed | kind:question`,
        already covered above).

    All in one go, deduped by id. Soft-fails — never blocks startup.
    A failed rehydrate just means the first fire reads less context,
    not a broken loop.
    """
    cutoff_iso = (datetime.now(UTC) - timedelta(hours=REHYDRATE_WINDOW_HOURS)).isoformat()

    # Each tag-filter pulls a slice of conversation substrate. Run in
    # parallel; merge by id below.
    queries = [
        # Q — composer seeds + openai-compat user turns + any other
        # puddle-shaped question intent
        ["kind:question"],
        # A — witness output: feed-card covers chat-reply, route:alert:*,
        # route:feed-card, route:claude-code dispatches, route:tool:*
        # proposals — every shape of Fathom-authored turn
        ["feed-card"],
        # Helper dispatched-task closures the watcher minted intents for
        ["helper-reply"],
        # Routine machinery — when a cron tripped, what it produced
        ["routine-fire"],
        ["routine-summary"],
        ["routine-due"],
    ]
    try:
        slices = await asyncio.gather(
            *[
                delta_client.query(
                    tags_include=tags,
                    time_start=cutoff_iso,
                    limit=REHYDRATE_MAX_TURNS,
                )
                for tags in queries
            ],
            return_exceptions=True,
        )
    except Exception as e:
        print(f"[rehydrate] lake fetch crashed: {type(e).__name__}: {e}")
        return

    written = 0
    seen_ids: set[str] = set()
    all_deltas: list[dict] = []
    for slice_result in slices:
        if isinstance(slice_result, Exception):
            print(f"[rehydrate] one slice failed: {type(slice_result).__name__}: {slice_result}")
            continue
        all_deltas.extend(slice_result or [])
    # Sort newest-last so the most recent turns get the latest puddle
    # write timestamps (preserves chronological feed order).
    all_deltas.sort(key=lambda d: d.get("timestamp") or "")

    for d in all_deltas:
        did = d.get("id") or ""
        if not did or did in seen_ids:
            continue
        seen_ids.add(did)
        content = (d.get("content") or "").strip()
        if not content:
            continue
        src_tags = list(d.get("tags") or [])
        # Stamp puddle scope + recalled-id dedup so subsequent reads
        # know this was a rehydrated copy (and any future telepathy-
        # like restore would dedupe correctly via recalled-id).
        if CONVO_TAG not in src_tags:
            src_tags.append(CONVO_TAG)
        short = did[:24]
        if not any(t.startswith("recalled-id:") for t in src_tags):
            src_tags.append(f"recalled-id:{short}")
        try:
            await puddle.write(
                content=content,
                tags=src_tags,
                source=d.get("source") or "rehydrate",
                ttl_seconds=REHYDRATE_WINDOW_HOURS * 3600,
                timestamp=d.get("timestamp"),
                embedding=d.get("embedding") or None,
            )
            written += 1
        except Exception as e:
            print(f"[rehydrate] puddle write failed for {did[:12]}: {type(e).__name__}: {e}")

    print(f"[rehydrate] seeded {written} conversation delta(s) from last {REHYDRATE_WINDOW_HOURS}h")


def start() -> None:
    """Start supervisor + reaper + pressure-watcher +
    claude-code-watcher + feed-orient regen. Idempotent.

    Cold-start rehydrate runs as a fire-and-forget task — pulls a few
    hours of recent conversation turns into the puddle so the first
    post-restart fire has context.
    """
    global _reaper_task, _pressure_task, _helper_task, _boot_iso
    if _reaper_task is not None:
        return
    _boot_iso = _now_iso()
    asyncio.create_task(rehydrate_puddle(), name="loop/rehydrate")
    _reaper_task = asyncio.create_task(_reaper(), name="loop/reaper")
    _pressure_task = asyncio.create_task(pressure_watcher(), name="loop/pressure")
    _helper_task = asyncio.create_task(claude_code_watcher_loop(), name="loop/helper-watcher")
    feed_orient.start()

    from . import threaded_supervisor

    threaded_supervisor.start()


async def stop() -> None:
    """Cancel all background tasks. Idempotent."""
    global _reaper_task, _pressure_task, _helper_task
    await feed_orient.stop()
    from . import threaded_supervisor

    await threaded_supervisor.stop()
    for task in (
        _reaper_task,
        _pressure_task,
        _helper_task,
    ):
        if task is None:
            continue
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    _reaper_task = None
    _pressure_task = None
    _helper_task = None
