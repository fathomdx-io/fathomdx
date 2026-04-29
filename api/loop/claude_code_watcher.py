"""Claude-code watcher — turn live claude-code session activity into
loop intents.

A `task-spawn` delta (written by the kitty plugin once it learns the
spawned session's id) declares that claude-code session `<sid>` on
host `<H>` is now correlated to task `<corr>`. From then until a
matching `task-complete` lands, this watcher mints a `kind:claude-code-reply`
intent into the puddle for every assistant-role hook delta from that
session.

Each intent carries `channel:claude-code` + `claude-code-session:<corr>`,
so the supervisor groups them like any other channel-correlated intent
and the witness's reply naturally addresses them via
`addresses:<intent-id>` (which `pending_intents()` already filters on).
The closure dance — witness writing `task-complete,task-corr:<corr>`
when the task is done — is in `witness.py` (Phase 4).

User-role deltas (Fathom's own injections, echoed back by the hook)
are skipped via the `assistant` tag filter — minting on them would
loop the system on its own prompt.

Scope: this module only watches sessions that already have a
`task-spawn` join delta. Free-floating claude-code sessions (the user
working in their own terminal, not dispatched by Fathom) don't
register as tasks and don't fire the loop. That's a separate feature
(spontaneous DMs from claude-code) — not part of this branch.
"""

from __future__ import annotations

import asyncio

from .. import delta_client
from ..channels import channel_tag, correlation_tag
from .intents import write_intent
from .puddle import puddle


# Polling cadence. 5s feels like a real conversation latency without
# spamming the lake — claude turns take seconds-to-minutes, so a tighter
# poll wouldn't catch more substrate.
CLAUDE_CODE_POLL_S = 5.0

# Per-correlation: ISO timestamp of the most recent assistant delta we
# minted an intent for. Prevents re-minting the same Stop delta on each
# tick. Pre-populated at startup from existing puddle intents, so an
# API restart doesn't re-fire the loop on already-handled replies.
_last_minted: dict[str, str] = {}


def _tag_value(tags: list[str], prefix: str) -> str:
    for t in tags:
        if t.startswith(prefix):
            return t[len(prefix):]
    return ""


async def _active_correlations() -> dict[str, dict]:
    """Build the active task set from the lake.

    Returns ``{corr: {claude_session_id, host, project, spawn_iso}}``
    for each task-spawn delta whose correlation has no matching
    task-complete.
    """
    spawns, completes = await asyncio.gather(
        delta_client.query(tags_include=["task-spawn"], limit=200),
        delta_client.query(tags_include=["task-complete"], limit=200),
    )

    completed: set[str] = set()
    for c in completes:
        corr = _tag_value(c.get("tags") or [], "task-corr:")
        if corr:
            completed.add(corr)

    active: dict[str, dict] = {}
    for s in spawns:
        tags = s.get("tags") or []
        corr = _tag_value(tags, "task-corr:")
        sid = _tag_value(tags, "claude-code-session:")
        if not corr or not sid or corr in completed:
            continue
        # First-write-wins per corr — if the kitty plugin somehow wrote
        # multiple spawn deltas for one task (it shouldn't), the first
        # one establishes the binding and subsequent ones are ignored.
        if corr in active:
            continue
        active[corr] = {
            "claude_session_id": sid,
            "host": _tag_value(tags, "host:"),
            "project": _tag_value(tags, "project:"),
            "spawn_iso": s.get("timestamp") or "",
        }
    return active


async def _prime_last_minted() -> None:
    """Fill `_last_minted` from existing puddle intents at startup so
    we don't re-fire the loop on assistant deltas the watcher already
    minted in a previous process.
    """
    try:
        existing = puddle.query(
            tags_include=["intent", "kind:claude-code-reply"],
            limit=200,
        )
    except Exception as e:
        print(f"[claude-code watcher] prime failed: {type(e).__name__}: {e}")
        return
    for it in existing:
        tags = it.get("tags") or []
        corr = _tag_value(tags, f"claude-code-session:")
        if not corr:
            continue
        ts = it.get("timestamp") or ""
        if ts and ts > _last_minted.get(corr, ""):
            _last_minted[corr] = ts


async def claude_code_watcher_tick() -> None:
    """One pass: scan active sessions for new assistant deltas, mint
    intents."""
    active = await _active_correlations()
    if not active:
        return

    for corr, info in active.items():
        sid = info["claude_session_id"]
        # First mint from a session reaches back to the spawn timestamp;
        # later ticks pick up where we left off.
        last = _last_minted.get(corr) or info["spawn_iso"]

        try:
            replies = await delta_client.query(
                tags_include=["assistant", f"session:{sid}"],
                time_start=last,
                limit=50,
            )
        except Exception as e:
            print(
                f"[claude-code watcher] query failed for corr {corr[:12]}: "
                f"{type(e).__name__}: {e}"
            )
            continue

        # Sort oldest-first so multiple new replies in one tick mint as
        # separate intents in the order they actually happened.
        replies.sort(key=lambda d: d.get("timestamp") or "")

        for r in replies:
            ts = r.get("timestamp") or ""
            # `time_start` is inclusive on the lake — without the strict
            # `>` here we'd re-mint the boundary delta every tick.
            if ts <= last:
                continue
            content = (r.get("content") or "").strip()
            if not content:
                continue
            extra_tags = [
                channel_tag("claude-code"),
                correlation_tag("claude-code", corr),
                f"task-corr:{corr}",
                f"claude-code-session:{sid}",
                f"reply-to:{r.get('id') or ''}",
            ]
            if info.get("host"):
                extra_tags.append(f"host:{info['host']}")
            if info.get("project"):
                extra_tags.append(f"project:{info['project']}")
            try:
                await write_intent(
                    kind="claude-code-reply",
                    content=content,
                    extra_tags=extra_tags,
                    source="claude-code-watcher",
                )
                _last_minted[corr] = ts
                print(
                    f"[claude-code watcher] minted intent for corr "
                    f"{corr[:12]} from delta {(r.get('id') or '?')[:8]}"
                )
            except Exception as e:
                print(
                    f"[claude-code watcher] write_intent failed: "
                    f"{type(e).__name__}: {e}"
                )


async def claude_code_watcher_loop() -> None:
    """Background task — periodic claude_code_watcher_tick().

    Primes `_last_minted` once at startup so an API restart doesn't
    re-fire the loop on already-processed replies, then ticks forever.
    """
    await _prime_last_minted()
    print(f"[claude-code watcher armed] poll={CLAUDE_CODE_POLL_S}s")
    while True:
        try:
            await asyncio.sleep(CLAUDE_CODE_POLL_S)
        except asyncio.CancelledError:
            return
        try:
            await claude_code_watcher_tick()
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[claude-code watcher] crashed: {type(e).__name__}: {e}")
