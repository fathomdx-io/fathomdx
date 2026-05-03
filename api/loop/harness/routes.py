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
from . import run_harness


router = APIRouter()


def _sse(event: str, data: Any) -> str:
    """Format one SSE message. `event:` line lets the client switch
    on type without parsing the payload first."""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


@router.get("/v1/harness/test")
async def harness_test(question: str, request: Request) -> StreamingResponse:
    """Run a single harness fire end-to-end and stream the trace.

    `question` is the synthetic intent text. We seed it as a
    `kind:question` delta in the puddle (no channel tag, so it stays
    off external surfaces), call `run_harness`, and stream every
    callback event back as SSE. Resulting cards land in the lake +
    puddle the same way a real fire does — visible in the dashboard
    feed afterward.
    """
    question = (question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if len(question) > 2000:
        raise HTTPException(status_code=400, detail="question is too long (max 2000 chars)")

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
