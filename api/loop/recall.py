"""Recall — the searcher tick.

After each parliament round (and the witness pass that follows), fire
one recall tick: pick the latest voice thought from this session,
compose a 5-15 word query via a cheap LLM call, run that query against
the durable lake via delta_client.search(), and write the top hits as
`recall-result` deltas into the puddle. The loop's NEXT fire reads
those recall-results as part of its substrate via the witness's
anchors_block — voices drive the searcher, the searcher feeds the
voices on the next turn.

Lifted from experiments/loop-experiment/worker/controller.py:
  * compose_search_query (line 1183)
  * run_searcher_voice_tick (line 1216)
  * write_recall_to_lake (line 719)

Two key differences from the experiment:
  * No HTTP — we share-process delta_client directly.
  * Same-as-last-query dedupe lives in this module (was a global there).
  * Skips silently when the query is the same shape as the last fire
    (avoids hitting the lake for "what was that thing" twice in a row).
"""

from __future__ import annotations

import re

from .. import delta_client
from .intents import CONVO_TAG
from .llm import loop_generate
from .puddle import puddle


# Recall TTL — matches the rolling 48h horizon every other puddle item
# uses, so a recall surfaced this hour is still resonant 30 hours later.
RECALL_TTL_S = 48 * 60 * 60

# How many recall results to keep per searcher fire. The experiment
# used 5, but voices in our setup actually read recall-results as part
# of their substrate (process._gather_substrate), so a fatter pull gives
# the deliberation more material to draw from across rounds. K=10 is a
# moderate increase — the voice's INPUT_SAMPLE_K=8 budget caps how much
# actually lands in any one prompt, so this just widens the pool.
RECALL_LIMIT = 10

# Skip a query that's identical (post-normalize) to the last one fired
# in this process. Saves a redundant search when two voices in one
# round produce semantically-identical queries.
_last_query_norm: str | None = None


async def _compose_search_query(voice_name: str, thought_text: str) -> str | None:
    """One LLM call: return a 5-15 word query that would surface the
    most useful prior moments to inform the next round of deliberation.
    """
    text = (thought_text or "").strip()
    if not text:
        return None
    prompt = f"""A voice in Fathom's parliament — the **{voice_name}** voice — just contributed this thought:

  {text[:600]}

What ONE concise lake search query (5–15 words) would surface the most useful prior moments from Fathom's memory to inform what this voice or the others might say next?

Return JUST the query text. No quotes, no preamble, no labels."""
    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="medium",
            max_tokens=120,
            temperature=0.5,
        )
    except Exception:
        return None
    q = (raw or "").strip().strip('"\'`')
    return q if q else None


async def _write_recall_results(
    *,
    session_tag: str,
    results: list[dict],
    event_id: str,
) -> int:
    """Write each recall hit into the puddle as a `recall-result` delta.

    Dedupe: skip if a previous recall in this convo already mirrored
    the same lake delta id. Append-only — the puddle never loses a
    recall, but doesn't re-import duplicates.
    """
    # Collect ids already mirrored as recalls in this convo.
    existing_ids: set[str] = set()
    for d in puddle.query(tags_include=[CONVO_TAG, "recall-result"], limit=500):
        for t in d.get("tags") or []:
            if t.startswith("recalled-id:"):
                existing_ids.add(t.split(":", 1)[1])

    written = 0
    for r in results:
        if isinstance(r, dict) and "delta" in r:
            d = r["delta"] or {}
            klass = r.get("klass") or "first"
        else:
            d = r or {}
            klass = "first"
        content = (d.get("content") or "").strip()
        if not content:
            continue
        original_id = d.get("id") or ""
        recalled_short = original_id[:24] if original_id else ""
        if recalled_short and recalled_short in existing_ids:
            continue
        original_source = d.get("source") or "unknown"
        tags = [
            CONVO_TAG, session_tag,
            "recall-result", "mirror",
            f"recall-event:{event_id}",
            f"recall-class:{klass}",
            f"from-source:{original_source}",
        ]
        if recalled_short:
            tags.append(f"recalled-id:{recalled_short}")
            existing_ids.add(recalled_short)
        await puddle.write(
            content=content,
            tags=tags,
            source=f"recall:{original_source}",
            ttl_seconds=RECALL_TTL_S,
        )
        written += 1
    return written


async def run_searcher_tick(*, session_tag: str, event_id: str) -> int:
    """Fire one searcher pass: pick the latest voice thought in this
    session, compose a query, recall against the lake, write hits
    into the puddle. Returns the number of recall-results written.

    Called from worker.py after each parliament round (or a fixed
    cadence; the experiment fired it inline as a fourth voice in the
    rotation). Soft-fails on any single-step error — a hiccup in one
    fire shouldn't block the loop.
    """
    global _last_query_norm

    # Latest voice thought in this session (newest-first from query).
    thoughts = puddle.query(
        tags_include=[CONVO_TAG, session_tag, "thought"],
        limit=1,
    )
    if not thoughts:
        return 0
    latest = thoughts[0]
    text = (latest.get("content") or "").strip()
    voice_name = next(
        (t.split(":", 1)[1] for t in (latest.get("tags") or []) if t.startswith("voice:")),
        "voice",
    )

    query = await _compose_search_query(voice_name, text)
    if not query:
        return 0
    norm = re.sub(r"\s+", " ", query.lower().strip())
    if norm == _last_query_norm:
        print(f"[searcher] same query as last — skipping recall: {query[:60]!r}")
        return 0
    _last_query_norm = norm

    print(f"[searcher] {voice_name}: {query[:80]!r}")
    try:
        recall = await delta_client.search(query=query, limit=RECALL_LIMIT)
    except Exception as e:
        print(f"[searcher] lake search failed: {type(e).__name__}: {e}")
        return 0

    results = recall.get("results") or []
    n = await _write_recall_results(
        session_tag=session_tag,
        results=results,
        event_id=event_id,
    )
    if n:
        print(f"[searcher] wrote {n} recall-result(s) into the puddle")
    return n
