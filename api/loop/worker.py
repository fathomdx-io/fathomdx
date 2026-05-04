"""Grand Loop supervisor.

Polls the puddle for pending intents. When any are present, hands the
fire to the agentic harness — the model elects its own deliberation
via tool calls (semantic / state / pattern / time / relate / etc.),
produces a response, and dispatches the card.

The convener+parliament+witness path was retired in the harness
migration; the harness's `deliberate` tool covers the antagonism case
when needed. The intent-searcher pre-pass was retired in Phase 9 — it
was a parliament-era artifact that forced a search the harness's
voices needed but the harness itself doesn't (the harness can call
`semantic` if it wants substrate).
"""

from __future__ import annotations

import asyncio
import uuid

from datetime import UTC, datetime, timedelta

from .. import delta_client
from .. import standpoint as standpoint_mod
from . import feed_orient
from .claude_code_watcher import claude_code_watcher_loop
from .harness import run_harness
from .intents import CONVO_TAG, next_intent_group, pending_intents
from .pressure import pressure_watcher
from .puddle import puddle


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


REHYDRATE_WINDOW_HOURS = 6
REHYDRATE_MAX_TURNS = 30


async def rehydrate_puddle() -> None:
    """Cold-start: seed the puddle with recent conversation turns from
    the lake so the first post-restart fire has substrate context.

    Without telepathy mirroring, an api restart leaves the puddle
    empty — the harness fires from a cold context. This pulls the
    last few hours of conversation deltas (user messages + witness
    cards + claude-code closures + routine fires) and pending intents,
    enough to give the next fire a coherent recent-history view.

    Soft-fails — never blocks startup. A failed rehydrate just means
    the first fire reads less context, not a broken loop.
    """
    cutoff_iso = (datetime.now(UTC) - timedelta(hours=REHYDRATE_WINDOW_HOURS)).isoformat()
    try:
        # Pull conversation turns: user messages (kind:question intents
        # or user-seed deltas) + witness feed-cards. These are the only
        # things the puddle should hold per the post-migration model
        # (substrate = conversation feed; everything else via tools).
        seeds, cards = await asyncio.gather(
            delta_client.query(
                tags_include=["kind:question"],
                time_start=cutoff_iso,
                limit=REHYDRATE_MAX_TURNS,
            ),
            delta_client.query(
                tags_include=["feed-card"],
                time_start=cutoff_iso,
                limit=REHYDRATE_MAX_TURNS,
            ),
        )
    except Exception as e:
        print(f"[rehydrate] lake fetch crashed: {type(e).__name__}: {e}")
        return

    written = 0
    seen_ids: set[str] = set()
    for d in (seeds or []) + (cards or []):
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

    Telepathy was retired — the puddle is the conversation feed only.
    Identity / mood / recent-alerts that the harness needs come from
    standpoint (lake-direct reads) at fire start; the dashboard's
    Identity surfaces read directly from /v1/moods/history and
    /v1/crystal/events. No background mirror needed.

    Cold-start rehydrate runs as a fire-and-forget task — pulls a few
    hours of recent conversation turns into the puddle so the first
    post-restart fire has context.
    """
    global _supervisor_task, _reaper_task, _pressure_task
    global _claude_code_task, _boot_iso
    if _supervisor_task is not None:
        return
    _boot_iso = _now_iso()
    asyncio.create_task(rehydrate_puddle(), name="loop/rehydrate")
    _supervisor_task = asyncio.create_task(_supervisor(), name="loop/supervisor")
    _reaper_task = asyncio.create_task(_reaper(), name="loop/reaper")
    _pressure_task = asyncio.create_task(pressure_watcher(), name="loop/pressure")
    _claude_code_task = asyncio.create_task(
        claude_code_watcher_loop(), name="loop/claude-code-watcher"
    )
    feed_orient.start()


async def stop() -> None:
    """Cancel all background tasks. Idempotent."""
    global _supervisor_task, _reaper_task, _pressure_task
    global _claude_code_task
    await feed_orient.stop()
    for task in (
        _supervisor_task,
        _reaper_task,
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
    _pressure_task = None
    _claude_code_task = None
