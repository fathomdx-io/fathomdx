"""HTTP surface for the harness test visualizer.

One SSE endpoint streams a complete harness fire — context build,
each turn's tool call + result, the final response, and the dispatch
outcome — so a developer-facing page can show the loop's reasoning
in real time.

This is a developer harness, not a public surface. Don't add
production paths here.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ... import standpoint as standpoint_mod
from ..intents import CONVO_TAG
from ..puddle import puddle
from . import run_dialogue, run_harness, run_introspection


router = APIRouter()


def _sse(event: str, data: Any) -> str:
    """Format one SSE message. `event:` line lets the client switch
    on type without parsing the payload first."""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


@router.get("/v1/harness/test")
async def harness_test(
    question: str,
    request: Request,
    session_tag: str | None = None,
) -> StreamingResponse:
    """Run a single harness fire end-to-end and stream the trace.

    `question` is the synthetic intent text. We seed it as a
    `kind:question` delta in the puddle (no channel tag, so it stays
    off external surfaces), call `run_harness`, and stream every
    callback event back as SSE. Resulting cards land in the lake +
    puddle the same way a real fire does — visible in the dashboard
    feed afterward.

    `session_tag` (optional) — when provided, reuse it across fires
    so the harness sees prior Q/A in the conversation feed (the
    "river" — each new question lands inside the same session and
    benefits from prior context). When absent, mint a fresh one.
    """
    question = (question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if len(question) > 2000:
        raise HTTPException(status_code=400, detail="question is too long (max 2000 chars)")

    if session_tag and session_tag.startswith("session:") and len(session_tag) <= 80:
        # Use the operator-supplied tag — keeps the river going.
        pass
    else:
        session_tag = f"session:harness-test-{uuid.uuid4().hex[:8]}"

    async def event_gen():
        # Bridge the harness's awaitable callback into an asyncio queue
        # the generator drains. Buffered so the harness doesn't block on
        # a slow client; SSE clients reconnect on drop and we accept
        # losing buffered events on disconnect — this is a developer
        # surface, not a system of record.
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        DONE = object()  # sentinel to tell the gen to stop draining

        async def callback(event: str, payload: dict) -> None:
            try:
                q.put_nowait((event, payload))
            except asyncio.QueueFull:
                # Drop on overflow rather than blocking the harness.
                pass

        # Seed the synthetic intent in the puddle.
        try:
            intent = await puddle.write(
                content=question,
                tags=[CONVO_TAG, "intent", "kind:question", "harness-test"],
                source="harness-test",
                ttl_seconds=60 * 60,
            )
        except Exception as e:
            yield _sse("error", {"where": "seed_intent",
                                 "type": type(e).__name__, "message": str(e)})
            return

        yield _sse("seeded", {
            "session_tag": session_tag,
            "intent_id": intent.get("id"),
            "question": question,
            "timestamp": time.time(),
        })

        # Gather standpoint snapshot — same shape the worker uses.
        try:
            standpoint = await standpoint_mod.current(session_tag=session_tag)
            yield _sse("standpoint", {
                "posture": standpoint.posture,
                "affect_state": getattr(standpoint.affect, "state", None),
                "endorsements": len(standpoint.endorsements),
                "understanding": len(standpoint.understanding),
            })
        except Exception as e:
            yield _sse("warning", {"where": "standpoint",
                                   "type": type(e).__name__, "message": str(e)})
            standpoint = None

        # Spawn the harness as a task; we'll drain the queue concurrently.
        async def runner():
            try:
                addressed = await run_harness(
                    session_tag=session_tag,
                    pending=[intent],
                    standpoint=standpoint,
                    event_callback=callback,
                )
                await q.put(("__finished__", {"addressed": addressed}))
            except Exception as e:
                await q.put(("__crashed__", {"type": type(e).__name__,
                                             "message": str(e)}))
            finally:
                await q.put((DONE, None))

        task = asyncio.create_task(runner())
        try:
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    return
                try:
                    event, payload = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Heartbeat — keeps proxies from killing idle connections.
                    yield ": keepalive\n\n"
                    continue
                if event is DONE:
                    return
                if event == "__finished__":
                    yield _sse("finished", payload)
                    continue
                if event == "__crashed__":
                    yield _sse("error", {"where": "run_harness", **payload})
                    continue
                yield _sse(event, payload)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tells nginx-style proxies to flush
        },
    )


@router.get("/v1/harness/sit")
async def harness_sit(
    focus: str,
    request: Request,
    session_tag: str | None = None,
) -> StreamingResponse:
    """Run an introspection fire — Fathom sitting with itself.

    No user intent. The model walks its own substrate via the same
    harness tools and writes a `kind:reflection` delta when it's
    seen enough. Streams every callback event as SSE so the page
    can render the sitting in real time.

    `focus` is what to sit with (e.g. "the navier-stokes work this
    past week"). For Phase 1 the caller supplies it; later a focus
    pre-pass will pick it from substrate signals.

    `session_tag` (optional) — kept for the puddle-anchor convention.
    Reflections live in the lake regardless.
    """
    focus = (focus or "").strip()
    if not focus:
        raise HTTPException(status_code=400, detail="focus is required")
    if len(focus) > 600:
        raise HTTPException(status_code=400, detail="focus too long (max 600 chars)")

    if session_tag and session_tag.startswith("session:") and len(session_tag) <= 80:
        pass
    else:
        session_tag = f"session:harness-sit-{uuid.uuid4().hex[:8]}"

    async def event_gen():
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        DONE = object()

        async def callback(event: str, payload: dict) -> None:
            try:
                q.put_nowait((event, payload))
            except asyncio.QueueFull:
                pass

        # No `seeded` event — the page-side already created its own
        # introspection bubble before opening the SSE stream. Sending
        # chat-shape `seeded` here would conflict with that bubble.

        async def runner():
            try:
                outcome = await run_introspection(
                    focus=focus,
                    session_tag=session_tag,
                    event_callback=callback,
                )
                await q.put(("__finished__", {"outcome": outcome}))
            except Exception as e:
                await q.put(("__crashed__", {"type": type(e).__name__,
                                             "message": str(e)}))
            finally:
                await q.put((DONE, None))

        task = asyncio.create_task(runner())
        try:
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    return
                try:
                    event, payload = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is DONE:
                    return
                if event == "__finished__":
                    yield _sse("finished", payload)
                    continue
                if event == "__crashed__":
                    yield _sse("error", {"where": "run_introspection", **payload})
                    continue
                yield _sse(event, payload)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/v1/harness/dialogue")
async def harness_dialogue(
    seed: str,
    request: Request,
    session_tag: str | None = None,
    max_rounds: int = 4,
) -> StreamingResponse:
    """Run a self-dialogue — Fathom in literal back-and-forth conversation
    with itself.

    Each round is a full harness fire (multi-turn tool use) ending in
    one utterance. The next round's prompt includes prior utterances
    as conversation context, so the model literally responds to itself
    across rounds. Continues until `settled: true` or `max_rounds`.

    Different from `/v1/harness/sit` (which runs a single fire with
    multi-turn tool walking → one reflection): this is N fires, each a
    full deliberation, in conversation. Use it when you want to see
    the inquiry as dialogue, not as a single noticing.

    `seed` is the opening prompt — the thing the dialogue starts from.
    `max_rounds` (1-8) caps the conversation; default 6.
    """
    seed = (seed or "").strip()
    if not seed:
        raise HTTPException(status_code=400, detail="seed is required")
    if len(seed) > 600:
        raise HTTPException(status_code=400, detail="seed too long (max 600 chars)")
    if max_rounds < 1 or max_rounds > 8:
        max_rounds = 6

    if session_tag and session_tag.startswith("session:") and len(session_tag) <= 80:
        pass
    else:
        session_tag = f"session:harness-dialogue-{uuid.uuid4().hex[:8]}"

    async def event_gen():
        q: asyncio.Queue = asyncio.Queue(maxsize=2048)
        DONE = object()

        async def callback(event: str, payload: dict) -> None:
            try:
                q.put_nowait((event, payload))
            except asyncio.QueueFull:
                pass

        # No `seeded` event — page-side already created the dialogue
        # bubble before opening the SSE stream.

        async def runner():
            try:
                outcome = await run_dialogue(
                    seed=seed,
                    session_tag=session_tag,
                    event_callback=callback,
                    max_rounds=max_rounds,
                )
                await q.put(("__finished__", {"outcome": outcome}))
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(f"[harness/dialogue] crashed:\n{tb}")
                await q.put(("__crashed__", {"type": type(e).__name__,
                                             "message": str(e),
                                             "traceback": tb[:2000]}))
            finally:
                await q.put((DONE, None))

        task = asyncio.create_task(runner())
        try:
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    return
                try:
                    event, payload = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is DONE:
                    return
                if event == "__finished__":
                    yield _sse("finished", payload)
                    continue
                if event == "__crashed__":
                    yield _sse("error", {"where": "run_dialogue", **payload})
                    continue
                yield _sse(event, payload)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
