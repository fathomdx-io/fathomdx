#!/usr/bin/env python3
"""Standalone harness validator.

Drops a synthetic intent into the puddle, calls `run_harness` directly,
and prints the trace + resulting cards. Runs against the live api stack
(uses the same delta-store + LLM provider the real loop does), so it
exercises the full path end-to-end without touching the worker loop.

Run from the fathomdx repo root with the api stack already up:

    python scripts/harness_test.py "what's the weather like in St. Louis?"

The intent is written to the puddle and addressed by the harness; it
won't propagate to other surfaces because we don't tag it with a
channel. Lake writes the harness produces (witness card, attestation,
mood-shift, citations) ARE durable — they'll show up in the dashboard
feed under the witness source if you scroll back.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

# Make the api package importable when running this script directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api import standpoint as standpoint_mod  # noqa: E402
from api.loop.harness import run_harness  # noqa: E402
from api.loop.intents import CONVO_TAG  # noqa: E402
from api.loop.puddle import puddle  # noqa: E402
from api.loop.recall import run_intent_searcher_tick  # noqa: E402


async def _seed_intent(text: str) -> dict:
    """Write a synthetic question intent into the puddle so the harness
    has something to address. No channel tags — keeps the response off
    every surface (dashboard feed will still show the resulting card)."""
    return await puddle.write(
        content=text,
        tags=[CONVO_TAG, "intent", "kind:question", "harness-test"],
        source="harness-test",
        ttl_seconds=60 * 60,  # 1h is plenty for a test fire
    )


async def _main(question: str) -> int:
    session_tag = f"session:harness-test-{uuid.uuid4().hex[:8]}"
    print("\n=== harness test ===")
    print(f"session: {session_tag}")
    print(f"question: {question!r}\n")

    intent = await _seed_intent(question)
    print(f"seeded intent: id={intent.get('id')!r}\n")

    try:
        standpoint = await standpoint_mod.current(session_tag=session_tag)
        print(
            f"standpoint: posture={standpoint.posture} "
            f"affect={standpoint.affect.state} "
            f"endorsements={len(standpoint.endorsements)} "
            f"understanding={len(standpoint.understanding)}\n"
        )
    except Exception as e:
        print(f"standpoint gather crashed: {type(e).__name__}: {e}\n")
        standpoint = None

    # Mirror worker.py — seed the puddle with intent-anchored recall before
    # firing the harness. Without this, the harness starts cold with only
    # identity anchors; production fires always have a recall-result block
    # already in the puddle for the harness's _render_anchors path.
    try:
        await run_intent_searcher_tick(
            session_tag=session_tag,
            event_id=f"{session_tag.split(':', 1)[1]}-intent-seed",
            intents=[intent],
        )
        print("seeded recall via intent-searcher\n")
    except Exception as e:
        print(f"intent-searcher seed crashed: {type(e).__name__}: {e}\n")

    addressed = await run_harness(
        session_tag=session_tag,
        pending=[intent],
        standpoint=standpoint,
    )
    print(f"\n=== addressed: {addressed} ===\n")

    # Show the resulting witness card(s) from the puddle.
    cards = puddle.query(tags_include=[session_tag, "feed-card"], limit=5)
    if cards:
        print("=== cards landed ===\n")
        for c in cards:
            try:
                payload = json.loads(c.get("content") or "{}")
            except Exception:
                payload = {"body": c.get("content")}
            print(f"  route: {payload.get('route')}")
            print(f"  kicker: {payload.get('kicker')}")
            print(f"  title:  {payload.get('title')}")
            print(f"  body:   {payload.get('body')}")
            tail = payload.get("tail")
            if tail:
                print(f"  tail:   {tail}")
            print()
    else:
        print("(no cards landed — silent fire or dispatch failure)")

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "usage: python scripts/harness_test.py <question>\n"
            'example: python scripts/harness_test.py "what have we been working on lately?"',
            file=sys.stderr,
        )
        sys.exit(2)
    question = " ".join(sys.argv[1:])
    sys.exit(asyncio.run(_main(question)))
