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
import uuid

from .. import standpoint as standpoint_mod
from . import feed_orient
from .claude_code_watcher import claude_code_watcher_loop
from .convener import run_convener
from .intents import next_intent_group, pending_intents
from .metric import (
    emit_metric,
    measure_cross_voice_convergence,
    session_aware_spread_max,
    settle_window_check,
)
from .pressure import pressure_watcher
from .process import run_process
from .puddle import puddle
from .recall import run_intent_searcher_tick, run_voice_followup_tick
from .telepathy import telepathy_loop
from .voice_stances import stance_regen_watcher
from .witness import run_witness

# Maximum parliament rounds per fire. With settle detection in place
# the loop usually exits earlier — voices converge in 2-4 rounds when
# they're going to converge at all, and the witness fires the moment
# the rolling cross-voice spread tightens below SETTLE_SPREAD_MAX.
# MAX_ROUNDS is a safety cap so a deadlocked parliament can't run
# forever; the witness still fires after it (with a "did not settle"
# descriptor in the prompt) so an unresolved tension gets named honestly.
MAX_ROUNDS_PER_FIRE = 8

# Cap when the convener picks depth=minimal — a focused 1-2 voice pass
# that doesn't need the full antagonism cycle. Keeps the deliberation
# short so a single-angle question doesn't grind through eight rounds.
MAX_ROUNDS_MINIMAL = 2

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
_stance_regen_task: asyncio.Task | None = None
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

    # Standpoint — gather Fathom's self-state ONCE at fire start and
    # pass the same snapshot through every stage. This is the River
    # principle in code: the convener, voices, and witness all read
    # FROM one consistent self-view rather than each re-fetching mid-
    # fire and risking a torn read if a slow-clock regen lands part
    # way through. Soft-fails: any sub-loader exception inside
    # standpoint.current() yields an empty component, never propagates.
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

    convergence_samples: list[float] = []
    settled = False
    settle_level: float | None = None
    rounds_run = 0

    # Ground voices on the user's literal intent BEFORE round 0 so they
    # don't speculate in a vacuum. The voice-driven searcher inside the
    # round loop reads voice thoughts; without this seed, round 0 has no
    # recall-results at all and round 1+ searches paraphrase whatever
    # the voices already guessed. See recall.run_intent_searcher_tick.
    try:
        await run_intent_searcher_tick(
            session_tag=session_tag,
            event_id=f"{session_tag.split(':', 1)[1]}-intent-seed",
            intents=pending,
        )
    except Exception as e:
        print(f"[loop fire] intent-searcher seed crashed: {type(e).__name__}: {e}")

    # Convener — picks the parliament's shape for this fire. Reads the
    # intent + the recall just seeded above + the standpoint; returns
    # depth + voices. On any error path it falls back to the trimurti
    # at full depth, so this call can never block the loop.
    verdict = await run_convener(
        session_tag=session_tag, pending=pending, standpoint=standpoint
    )
    active_voices = verdict.voices
    voice_names = [v["name"] for v in active_voices]
    print(
        f"[loop fire] convener: depth={verdict.depth} "
        f"voices={voice_names} rationale={verdict.rationale!r}"
    )

    if verdict.depth == "zero" or not active_voices:
        # Skip parliament entirely — witness speaks from substrate alone.
        # Intent-searcher already pre-loaded recall; that plus identity
        # anchors plus mood is enough for casual drop-ins / small talk.
        print("[loop fire] depth=zero — skipping parliament")
    else:
        # Token-budget pass 2026-04-30: hard-cap parliament at 1 round at
        # all depths. Two voices x 1 round still produces meaningful
        # divergence; multi-round refinement was 4-7× the token cost for
        # marginal gain. The convergence metric below noops on a single
        # round (no spread to measure) — that's expected.
        rounds_cap = 1

        for round_idx in range(rounds_cap):
            rounds_run = round_idx + 1
            # Fire all voices in parallel for this round. Each voice reads
            # the puddle as it stands at round-start (telepathy mirrors +
            # any recall-results from the intent-seed pull above). The
            # per-voice followup searcher was retired in the token-budget
            # pass; voices reason on the intent-seed substrate plus
            # whatever resonance the witness's feed-window surfaces.
            voice_coros = [
                run_process(
                    pid=f"{round_idx}-{v['name']}-{uuid.uuid4().hex[:6]}",
                    session_tag=session_tag,
                    voice=v,
                    pending=pending,
                    peer_voices=active_voices,
                    standpoint=standpoint,
                )
                for v in active_voices
            ]
            results = await asyncio.gather(*voice_coros, return_exceptions=True)

            voice_thoughts: list[tuple[str, str]] = []
            for v, res in zip(active_voices, results):
                if isinstance(res, Exception):
                    print(f"[loop fire] {v['name']} crashed: {type(res).__name__}: {res}")
                    continue
                voice_thoughts.append((v["name"], res or ""))

            # Per-voice convergence sample — measured against the OTHER
            # voices' takes for this fire's active set. Append samples
            # in order so the rolling window's spread reflects this
            # round's spread.
            for voice_name, text in voice_thoughts:
                d = measure_cross_voice_convergence(
                    text=text,
                    voice_name=voice_name,
                    session_tag=session_tag,
                    voice_names=voice_names,
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

            # Settle check — exits when the last SETTLE_WINDOW samples
            # span less than the session-aware threshold. Phase 4b: the
            # threshold drifts based on recent puddle metric history —
            # hard-problem hours loosen it so the parliament isn't
            # banging against an unreachable convergence; quiet hours
            # tighten it so early ripples don't false-settle. Falls
            # back to the static SETTLE_SPREAD_MAX when there's not
            # enough recent history to drift confidently.
            ok, level = settle_window_check(
                convergence_samples,
                spread_max=session_aware_spread_max(),
            )
            if ok:
                settled = True
                settle_level = level
                break

        if settled:
            print(f"[loop fire] settled after {rounds_run} round(s) at level {settle_level:.2f}")
        else:
            print(f"[loop fire] did NOT settle after {rounds_run} round(s) (cap={rounds_cap})")

    try:
        await run_witness(
            session_tag=session_tag,
            pending=pending,
            voice_order=voice_names or None,
            standpoint=standpoint,
        )
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
    claude-code-watcher + feed-orient regen + stance-regen watcher.
    Idempotent."""
    global _supervisor_task, _reaper_task, _telepathy_task, _pressure_task
    global _claude_code_task, _stance_regen_task, _boot_iso
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
    # Phase 5c — slow-clock stance regen. Voices that accumulate
    # affirmations get their stance/bias text refined every ~6h.
    # This is the schedule for the activity built in Phase 5a.
    _stance_regen_task = asyncio.create_task(
        stance_regen_watcher(), name="loop/stance-regen"
    )
    feed_orient.start()


async def stop() -> None:
    """Cancel all background tasks. Idempotent."""
    global _supervisor_task, _reaper_task, _telepathy_task, _pressure_task
    global _claude_code_task, _stance_regen_task
    await feed_orient.stop()
    for task in (
        _supervisor_task,
        _reaper_task,
        _telepathy_task,
        _pressure_task,
        _claude_code_task,
        _stance_regen_task,
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
    _stance_regen_task = None
