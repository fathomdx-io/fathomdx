"""Chat listener — Fathom's lake-driven response loop.

Fathom's identity is "a distributed system that thinks, remembers, reflects,
acts, and speaks." Until now, Fathom only spoke in response to an HTTP
request. This module is the symmetric half: Fathom-the-mind lives in the
lake, listening for any new chat delta in any session and taking a turn
per delta.

The trigger layer is uniform: every new chat delta is a potential turn.
The response layer is where choice lives — Fathom can speak, or answer
with `<...>` to stay silent. Silence is the default; speaking is a choice.

One process, one listener. No distributed locks, no dedup protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from . import db, delta_client
from ._bgtasks import spawn as _spawn_task
from ._tags import tag_suffix

# The uvicorn default config doesn't raise app loggers above WARNING, so
# INFO lines from this module would be swallowed. We want the operational
# trail visible in `podman logs` for debugging turns, so pin this logger
# to INFO explicitly.
logging.getLogger(__name__).setLevel(logging.INFO)

# Short-term history window — last N messages fed back in as context.
# Mirrors the constant in server.py's chat_completions; keeping them in
# sync by convention since this listener is Fathom's other entry point.
SHORTTERM_TURNS = 6

log = logging.getLogger(__name__)

# How often the listener wakes up to check the lake. Short enough that
# conversation feels live; long enough to avoid spinning on an empty lake.
POLL_INTERVAL_SECONDS = 3

# Cap on the per-session lock cache so a long-running process that has
# seen many one-off sessions doesn't leak a Lock per session forever.
# 256 is comfortably larger than any realistic concurrent-session
# working set and still measures in kilobytes of memory.
_SESSION_LOCK_CAP = 256

# How long ephemeral chat-event deltas (tool uses, silent acks, image
# views) stick around in the lake before the delta-store reaps them.
# Long enough that a user who just switched tabs and comes back sees the
# trail; short enough that they don't accumulate and clutter queries.
EVENT_TTL_SECONDS = 300

# Sources whose deltas should NOT trigger a Fathom turn:
#   - fathom-chat-event: our own ephemeral tool/silence events.
#   - fathom-mood, fathom-feed: other consumer-api writes, side-effects,
#     not conversation.
# `fathom-chat` is NOT in this list: both user and Fathom messages use
# it; own-writes are filtered by the participant:fathom tag check below.
IGNORED_SOURCES = {
    "fathom-chat-event",
    "fathom-mood",
    "fathom-feed",
}

# Session-metadata deltas (renames, tombstones) carry source=fathom-chat
# and no participant tag, so the source + participant filters below
# can't catch them — they'd otherwise be treated as fresh user messages
# and trigger a spurious turn whose "user text" is the session title.
# db.get_messages already skips these same tags as metadata.
METADATA_TAGS = {"chat-name", "chat-deleted", "chat-view"}

# Memory-tool events no longer surface as chat-event deltas — the model's
# own <recalled>...</recalled> preamble on its reply is the durable
# provenance. Silence-ack and image-view events still flow through as
# before; only retrieval chatter is dropped.
_SUPPRESSED_TOOL_EVENTS = {"remember", "recall", "deep_recall"}

# Pattern for the provenance tag the model may emit at the top of its
# reply. Kept DOTALL so wrapped prose is accepted; kept non-greedy so a
# model that accidentally includes angle brackets later doesn't swallow
# the rest of the reply.
_RECALLED_RE = re.compile(r"<recalled>.*?</recalled>", re.DOTALL | re.IGNORECASE)


class ChatListener:
    """Polls the lake for new chat deltas and fires inference turns.

    Holds a single `last_seen` timestamp across all sessions. Starts at
    process boot time so a restart doesn't retrigger historical messages.
    Each tick: query deltas newer than last_seen, group by session, fire
    one turn per session (not one per delta — many deltas in a short
    window should produce one response, not many).
    """

    def __init__(self) -> None:
        # Start from "now" so a restart doesn't fire on historical deltas.
        # Future work: persist to disk so a crash mid-response doesn't
        # drop the trigger — for now, losing a turn on crash is acceptable.
        self._last_seen = datetime.now(UTC).isoformat()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Per-session locks so concurrent deltas in the same session are
        # processed serially — avoids overlapping inference for one chat.
        # Separate sessions can still run concurrently. The dict is bounded:
        # locks for the `_SESSION_LOCK_CAP` most-recently-active sessions
        # are kept, older ones are dropped. Any dropped session just gets
        # a fresh lock on its next tick, which is safe because dropping
        # only happens when the session hasn't been active — no concurrent
        # ticks are in-flight for it to race with.
        self._session_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        print(f"chat-listener: started (polling every {POLL_INTERVAL_SECONDS}s)", flush=True)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
        print("chat-listener: stopped", flush=True)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                log.exception("chat-listener: tick error: %s", e)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)

    async def _tick(self) -> None:
        # Pull every delta newer than last_seen. The delta-store supports
        # tag filters but we want any chat:* tag; the cheap approach is to
        # query by timestamp and filter in-process. Volume here is low.
        try:
            fresh = await delta_client.query(
                limit=200,
                time_start=self._last_seen,
            )
        except Exception as e:
            log.warning("chat-listener: query failed: %s", e)
            return

        if not fresh:
            return

        # Group deltas by session so one session's flurry of messages turns
        # into one response, not N. Grouping uses the chat:<slug> tag —
        # deltas without it aren't chat and are ignored entirely.
        by_session: dict[str, list[dict]] = {}
        max_ts = self._last_seen
        for d in fresh:
            ts = d.get("timestamp") or ""
            if ts > max_ts:
                max_ts = ts
            if ts <= self._last_seen:
                continue  # belt-and-suspenders — should be filtered by time_start
            # Skip our own conversation writes. Without this we'd loop.
            if d.get("source") in IGNORED_SOURCES:
                continue
            session_slug = _chat_slug(d.get("tags") or [])
            if not session_slug:
                continue
            # Skip deltas that are Fathom's own chat turns (defensive —
            # IGNORED_SOURCES catches them by source, but a manually written
            # delta tagged participant:fathom should also be skipped).
            if "participant:fathom" in (d.get("tags") or []):
                continue
            # OpenAI-compat system messages ride in as recorded wishes,
            # not triggers. The accompanying user delta (when present)
            # is what fires a turn; a lone system delta must not.
            if "participant:client-system" in (d.get("tags") or []):
                continue
            # Session-metadata deltas (rename, tombstone) look like
            # chat deltas to the coarse filters above but aren't turns.
            if METADATA_TAGS.intersection(d.get("tags") or []):
                continue
            by_session.setdefault(session_slug, []).append(d)

        self._last_seen = max_ts

        if not by_session:
            return

        # Fire turns in parallel across sessions, serial within.
        await asyncio.gather(
            *(self._process_session(slug, deltas) for slug, deltas in by_session.items()),
            return_exceptions=True,
        )

    def _lock_for_session(self, slug: str) -> asyncio.Lock:
        """LRU-get-or-create a Lock for `slug`, bounded by _SESSION_LOCK_CAP.

        Separate from _process_session so the eviction logic is unit-
        testable without touching the network. Each call bumps the slug
        to the end of the OrderedDict; when the dict exceeds the cap, the
        least-recently-accessed entries are evicted.
        """
        lock = self._session_locks.get(slug)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[slug] = lock
            while len(self._session_locks) > _SESSION_LOCK_CAP:
                self._session_locks.popitem(last=False)
        else:
            self._session_locks.move_to_end(slug)
        return lock

    async def _process_session(self, slug: str, new_deltas: list[dict]) -> None:
        # One lock per session so overlapping ticks don't race on the same
        # conversation.
        lock = self._lock_for_session(slug)
        async with lock:
            try:
                await self._take_turn(slug, new_deltas)
            except Exception as e:
                log.exception("chat-listener: session %s turn failed: %s", slug, e)

    async def _take_turn(self, slug: str, new_deltas: list[dict]) -> None:
        # Import here to avoid a circular import — server.py imports
        # from this module too (to start/stop the listener in lifespan).
        from .server import fathom_think

        # Latest message content from the new batch becomes the "user
        # message" sent to the LLM. Full session history gives context.
        new_deltas_sorted = sorted(new_deltas, key=lambda d: d.get("timestamp") or "")
        latest = new_deltas_sorted[-1]
        latest_content = (latest.get("content") or "").strip()
        if not latest_content:
            return

        # Addressee = the contact who sent the triggering delta. Fathom's
        # reply carries their `contact:<slug>` tag so future queries can
        # pull "everything I've said to Bob" in one shot — correspondence
        # tagged with who it was *to*, per docs/contact-spec.md §Tagging.
        addressee_slug = _contact_slug(latest.get("tags") or [])

        print(
            f"chat-listener: turn in {slug} ({len(new_deltas_sorted)} new deltas, "
            f"trigger source={latest.get('source')}, addressee={addressee_slug or '?'})",
            flush=True,
        )

        # Tool events (remember / recall / image_view / etc.) are
        # surfaced as short-lived deltas tagged with this session so the
        # UI's existing poll picks them up — same visual trail users had
        # when tool use streamed over SSE, now just routed through the
        # lake with a TTL. Deltas reap automatically after EVENT_TTL.
        def on_tool_event(kind: str, name: str, data: dict) -> None:
            if kind != "result":
                return
            if name in _SUPPRESSED_TOOL_EVENTS:
                return
            _spawn_task(
                write_chat_event(slug, name, data, contact_slug=addressee_slug),
                name=f"chat-event/{name}",
            )

        history_msgs = await db.get_messages(slug)
        # Map the session history into OpenAI-ish {role, content} pairs.
        # Any legacy 'agent' deltas in the lake (from before the routing
        # path was removed) are surfaced as assistant-side context with a
        # host annotation so Fathom reads them as its own prior speech.
        history: list[dict] = []
        for m in history_msgs:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "agent":
                host = m.get("host") or "body"
                role = "assistant"
                content = f"[from body {host}]\n{content}"
            if role in ("user", "assistant") and content:
                history.append({"role": role, "content": content})
        # Trim to short-term window — fathom_think itself also trims, but
        # being explicit here keeps the boundary visible.
        history = history[-SHORTTERM_TURNS:]

        # The last message IS the trigger — strip it from history so it's
        # not duplicated as both history and user_message.
        if history and history[-1].get("content", "").endswith(latest_content):
            history = history[:-1]

        messages = await fathom_think(
            user_message=latest_content,
            history=history,
            recall=True,
            session_slug=slug,
            on_tool_event=on_tool_event,
        )
        reply_text = (messages[-1].get("content") or "").strip() if messages else ""
        # A reply that is only a <recalled> preamble with no body is
        # still silence — the model peeked at memory but chose not to
        # speak. Strip the tag before the silence check so these read
        # as silent acks, not as deltas with just provenance.
        body_after_recall = _RECALLED_RE.sub("", reply_text).strip()
        if not body_after_recall or body_after_recall == "<...>":
            # Active silence — Fathom heard, chose not to speak. Write a
            # short-lived ack delta so the UI knows the turn happened.
            # No persistence value, just a live receipt.
            print(f"chat-listener: silence in {slug} (<...>) — writing ack", flush=True)
            try:
                await write_chat_event(slug, "silence", {}, contact_slug=addressee_slug)
                print(f"chat-listener: silence ack written for {slug}", flush=True)
            except Exception as e:
                print(f"chat-listener: silence ack failed: {type(e).__name__}: {e}", flush=True)
            return

        # Persist Fathom's reply the same way the old chat endpoint did.
        # db.add_message tags it with participant:fathom so the listener's
        # next tick skips it (own-writes filter), and with contact:<addressee>
        # so the correspondence carries who the reply was to.
        await db.add_message(slug, "assistant", reply_text, contact_slug=addressee_slug)
        await db.touch_session(slug)


def _chat_slug(tags: list[str]) -> str | None:
    return tag_suffix(tags, "chat:")


def _contact_slug(tags: list[str]) -> str | None:
    return tag_suffix(tags, "contact:")


async def write_chat_event(
    session_slug: str,
    kind: str,
    data: dict,
    ttl_seconds: int | None = None,
    contact_slug: str | None = None,
) -> None:
    """Drop an ephemeral chat-event delta into the session.

    Tool uses (remember/recall/see_image/etc.) and silent acks all go
    through this. It's a normal lake delta with an expires_at window so
    the delta-store's reap loop cleans it up — no separate transport,
    no in-memory state, the UI just polls /v1/sessions/{id} like
    always and renders events alongside messages.

    `ttl_seconds` overrides the default EVENT_TTL for events that need
    to outlive a quick tab-switch — e.g. routine proposals, where the
    user may come back hours later to confirm the form.

    Tag contract:
      fathom-chat           — so it's findable with the same chat query
      chat:<slug>           — session membership
      chat-event            — distinguishes from durable user/fathom messages
      event:<kind>          — the kind of thing that happened
      participant:fathom    — Fathom did this (keeps own-writes filter honest)
    """
    ttl = ttl_seconds if ttl_seconds is not None else EVENT_TTL_SECONDS
    expires_at = (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()
    tags = [
        "fathom-chat",
        f"chat:{session_slug}",
        "chat-event",
        f"event:{kind}",
        "participant:fathom",
    ]
    if contact_slug:
        tags.append(f"contact:{contact_slug}")
    # Extra fields per event shape — a media_hash for image views, a
    # count for recall/remember, etc. Callers pass whatever matters.
    content = json.dumps({"kind": kind, **data})
    await delta_client.write(
        content=content,
        tags=tags,
        source="fathom-chat-event",
        expires_at=expires_at,
    )


# Module-level singleton — one listener per process.
listener = ChatListener()
