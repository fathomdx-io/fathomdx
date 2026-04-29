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
from .intents import CONVO_TAG, Q_A_TTL_S, write_intent
from .puddle import puddle


async def _mirror_closure_to_puddle(closure: dict, info: dict, corr: str) -> None:
    """Dual-write the closure delta into the puddle so the feed
    surfaces it within seconds, instead of waiting up to 5 minutes
    for telepathy's next mirror pass.

    Telepathy will eventually try to mirror this same delta — the
    `recalled-id:<short>` tag is the dedup contract: telepathy
    indexes existing puddle entries by recalled-id at the start of
    each pass and skips lake deltas whose short id is already
    present. So this fast-path doesn't double-render once telepathy
    catches up.

    The puddle entry preserves the original tags (so the feed's
    `task-complete` branch in routes.py renders it as a
    `claude-code-reply`) plus the standard telepathy stamps
    (`convo:grand`, `lake-delta`, `recalled-id`, `from-source`).
    """
    closure_id = closure.get("id") or ""
    if not closure_id:
        return
    short = closure_id[:24]
    # If a matching puddle entry already exists (telepathy got here
    # first, or we mirrored this same closure on a prior tick),
    # skip — same dedup logic telepathy uses.
    try:
        existing = puddle.query(
            tags_include=[CONVO_TAG, f"recalled-id:{short}"],
            limit=1,
        )
    except Exception:
        existing = []
    if existing:
        return
    src_tags = list(closure.get("tags") or [])
    # Inject `host:<H>` from the task-spawn join — claude writes the
    # closure via `fathom delta write` and doesn't know its own host,
    # so the lake-side closure delta has no host tag. Without this
    # the feed's reply card renders as "? ← claude" instead of
    # "<host> ← claude".
    if info.get("host") and not any(t.startswith("host:") for t in src_tags):
        src_tags.append(f"host:{info['host']}")
    new_tags = src_tags + [
        CONVO_TAG,
        "lake-delta",
        f"from-source:claude-code:task",
        f"recalled-id:{short}",
    ]
    try:
        await puddle.write(
            content=closure.get("content") or "",
            tags=new_tags,
            source=closure.get("source") or "claude-code:task",
            ttl_seconds=Q_A_TTL_S,
        )
    except Exception as e:
        print(
            f"[claude-code watcher] closure puddle-mirror failed for "
            f"{corr[:12]}: {type(e).__name__}: {e}"
        )


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


async def _dispatch_contact_for_corr(corr: str) -> str:
    """Pull the addressed contact from the original dispatch card so
    the closure intent can propagate it forward.

    The witness card that started the task carries `for:<slug>` (the
    user who asked, threaded from the chain origin). When the watcher
    mints a closure intent for that task, this lets the closure intent
    be tagged `contact:<slug>` too — the witness's chat-reply on the
    closure then renders as "Fathom > <slug>" instead of bare "Fathom".

    Returns empty string when the dispatch can't be found or has no
    for-tag (e.g., feed-pulse-driven dispatches with no human author).
    """
    try:
        dispatches = await delta_client.query(
            tags_include=["route:claude-code", f"task-corr:{corr}"],
            limit=5,
        )
    except Exception as e:
        print(
            f"[claude-code watcher] dispatch lookup failed for {corr[:12]}: "
            f"{type(e).__name__}: {e}"
        )
        return ""
    for d in dispatches:
        for t in d.get("tags") or []:
            if t.startswith("for:"):
                return t.split(":", 1)[1]
    return ""


async def _correlation_state() -> tuple[dict[str, dict], dict[str, dict]]:
    """Build active and closing correlation maps from the lake.

    Returns ``(active, closing)``:

      * ``active``: ``{corr: {claude_session_id, host, project, spawn_iso}}``
        for spawned tasks with no matching closure yet — the watcher
        polls these for new assistant deltas to mint as intents.

      * ``closing``: same shape but augmented with ``closure_delta`` and
        ``contact`` (the original asker, when known) — spawned tasks
        whose closure delta has landed. The watcher mints ONE intent
        per closing correlation (deduped via _last_minted) so the loop
        wakes on the final task report, not just on intermediate Stop
        hooks.
    """
    spawns, completes = await asyncio.gather(
        delta_client.query(tags_include=["task-spawn"], limit=200),
        delta_client.query(tags_include=["task-complete"], limit=200),
    )

    completed_corr_to_delta: dict[str, dict] = {}
    for c in completes:
        corr = _tag_value(c.get("tags") or [], "task-corr:")
        if corr and corr not in completed_corr_to_delta:
            # Newest closure wins per corr — if a corr somehow has
            # multiple `task-complete` deltas, the most recent is the
            # one we want to mint from. delta_client.query returns
            # newest-first, so the first hit is right.
            completed_corr_to_delta[corr] = c

    active: dict[str, dict] = {}
    closing: dict[str, dict] = {}
    for s in spawns:
        tags = s.get("tags") or []
        corr = _tag_value(tags, "task-corr:")
        sid = _tag_value(tags, "claude-code-session:")
        if not corr or not sid:
            continue
        if corr in active or corr in closing:
            continue
        info = {
            "claude_session_id": sid,
            "host": _tag_value(tags, "host:"),
            "project": _tag_value(tags, "project:"),
            "spawn_iso": s.get("timestamp") or "",
        }
        closure = completed_corr_to_delta.get(corr)
        if closure:
            closing[corr] = {**info, "closure_delta": closure}
        else:
            active[corr] = info
    # `contact` is filled in lazily only for corrs we're about to mint
    # (inside the closing loop, gated by _last_minted). Enriching every
    # closing entry on every tick was an extra lake query per old corr
    # per 5s — pool-exhausting when test scaffolding piled up.
    return active, closing


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


def _build_intent_tags(corr: str, sid: str, info: dict, source_id: str, *, closure: bool) -> list[str]:
    tags = [
        channel_tag("claude-code"),
        correlation_tag("claude-code", corr),
        f"task-corr:{corr}",
        f"claude-code-session:{sid}",
        f"reply-to:{source_id}",
    ]
    if info.get("host"):
        tags.append(f"host:{info['host']}")
    if info.get("project"):
        tags.append(f"project:{info['project']}")
    if closure:
        # Witness reads this to know "the task already wrapped up;
        # acknowledge in chat rather than dispatching another turn."
        # Without this marker, route:claude-code on the witness reply
        # would respawn the task — see kitty plugin's gate.
        tags.append("closure:true")
        # Propagate the chain's original addressee onto the closure
        # intent so the witness's chat-reply lands as "Fathom > <slug>".
        contact = info.get("contact")
        if contact:
            tags.append(f"contact:{contact}")
    return tags


async def claude_code_watcher_tick() -> None:
    """One pass: scan active sessions for new assistant deltas, plus
    closure deltas for tasks that just wrapped, and mint intents."""
    active, closing = await _correlation_state()
    if not active and not closing:
        return

    # ── Active sessions: mint from new assistant Stop-hook deltas ──
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
            try:
                await write_intent(
                    kind="claude-code-reply",
                    content=content,
                    extra_tags=_build_intent_tags(corr, sid, info, r.get("id") or "", closure=False),
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

    # ── Closing sessions: mint ONCE from the closure delta ──
    # On a tasked dispatch, claude often runs entirely through tool
    # calls (WebFetch + the closure-delta-write Bash) and emits an
    # empty assistant Stop hook — no `assistant`-tagged delta exists
    # to mint from. The closure delta IS the final reply in that case,
    # and the loop should wake on it just the same.
    for corr, info in closing.items():
        closure = info["closure_delta"]
        closure_id = closure.get("id") or ""
        closure_ts = closure.get("timestamp") or ""
        if not closure_id or not closure_ts:
            continue
        if _last_minted.get(corr, "") >= closure_ts:
            continue
        # Lazy contact lookup — only when we're actually minting,
        # not on every tick for every old corr in the closing map.
        if "contact" not in info:
            info["contact"] = await _dispatch_contact_for_corr(corr)
        # Fast-path the closure into the puddle so the feed renders
        # claude's reply within the watcher's 5s tick instead of
        # waiting on telepathy's 5min cadence. Idempotent via
        # recalled-id; safe to call before or alongside intent mint.
        await _mirror_closure_to_puddle(closure, info, corr)
        sid = info["claude_session_id"]
        content = (closure.get("content") or "").strip()
        if not content:
            _last_minted[corr] = closure_ts
            continue
        try:
            await write_intent(
                kind="claude-code-reply",
                content=content,
                extra_tags=_build_intent_tags(corr, sid, info, closure_id, closure=True),
                source="claude-code-watcher",
            )
            _last_minted[corr] = closure_ts
            print(
                f"[claude-code watcher] minted CLOSURE intent for corr "
                f"{corr[:12]} from delta {closure_id[:8]}"
            )
        except Exception as e:
            print(
                f"[claude-code watcher] write_intent (closure) failed: "
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
