"""Resonance — semantic ranking of puddle items against a signal.

The Grand Loop's substrate is a pool, not a stream. Voices and the
witness shouldn't be context-stuffed with whatever's most recent —
they should pull what RESONATES with what they're considering.

Two halves:

  * `ensure_embeddings(deltas)` — lazy: for any puddle delta missing an
    `_embedding`, batch-embed via the lake's CLIP encoder and attach.
    Mutates the delta dicts in place. Cached on the dict itself, so
    every subsequent rank against the same delta is free.

  * `rank(query_text, candidates, top_k)` — embed the query once,
    cosine-score every candidate, return the top-k. Soft-degrades to
    the input order on any embedding failure (no crash, just no
    resonance signal that round).

Cosine similarity is computed in pure Python — the candidate counts
involved (low hundreds at most) don't justify pulling NumPy into the
loop's hot path. Embeddings come back as plain lists of floats.
"""

from __future__ import annotations

import asyncio
import math
import time

from .. import delta_client


# Content-keyed cache for query-text embeddings, with per-key locks so
# three voices firing the same signal text on round 0 collapse to one
# /embed call instead of three serialized ones. Bounded by an LRU-ish
# trim so a long-running process doesn't grow the dict unbounded;
# voice queries change every round, so most entries fall out within
# minutes anyway.
_QUERY_EMBED_CACHE_MAX = 256
_query_embed_cache: dict[str, tuple[float, list[float]]] = {}
_query_embed_locks: dict[str, asyncio.Lock] = {}


def _trim_query_embed_cache() -> None:
    if len(_query_embed_cache) <= _QUERY_EMBED_CACHE_MAX:
        return
    # Drop the oldest quarter by insertion timestamp — cheap and good
    # enough; the cache exists to dedupe within a few-second window.
    by_age = sorted(_query_embed_cache.items(), key=lambda kv: kv[1][0])
    for key, _ in by_age[: len(_query_embed_cache) // 4]:
        _query_embed_cache.pop(key, None)
        _query_embed_locks.pop(key, None)


async def _embed_query_cached(text: str) -> list[float] | None:
    """Embed `text` (truncated to CLIP's useful budget) with a content-
    keyed coalesce. Concurrent callers with the same text wait on one
    in-flight /embed call rather than racing three of them onto the
    delta-store's serialized CLIP queue."""
    key = text[:1000]
    cached = _query_embed_cache.get(key)
    if cached is not None:
        return cached[1]
    lock = _query_embed_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _query_embed_cache.get(key)
        if cached is not None:
            return cached[1]
        try:
            embs = await delta_client.embed([key])
        except Exception as e:
            print(f"[resonance] query embed failed: {type(e).__name__}: {e}")
            return None
        if not embs or not embs[0]:
            return None
        _query_embed_cache[key] = (time.monotonic(), embs[0])
        _trim_query_embed_cache()
        return embs[0]


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors. Returns 0.0
    if either vector is degenerate (zero magnitude or mismatched len)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


async def ensure_embeddings(deltas: list[dict]) -> int:
    """Attach `_embedding` to any delta in `deltas` that lacks one.

    Batches the network call — one /embed request for all uncached
    items. Returns the number of new embeddings written. Mutates the
    delta dicts in place; cached on the dict so future calls are no-ops.

    On embedding failure, leaves the missing items without `_embedding`
    set — the ranker treats those as zero-similarity (effectively
    deprioritized but not excluded). The loop must keep running.
    """
    needs: list[tuple[int, str]] = []
    for i, d in enumerate(deltas):
        if "_embedding" in d:
            continue
        text = (d.get("content") or "").strip()
        if not text:
            d["_embedding"] = []  # Cache the empty so we don't retry.
            continue
        # Long content gets truncated — CLIP has a token cap and even
        # if it didn't, the first few hundred chars carry most of the
        # semantic signal for our purposes.
        needs.append((i, text[:1000]))

    if not needs:
        return 0

    texts = [t for _, t in needs]
    try:
        embeddings = await delta_client.embed(texts)
    except Exception as e:
        print(f"[resonance] embed batch failed: {type(e).__name__}: {e}")
        # Cache empty so we don't hammer the lake retrying on the
        # same fire — next reap clears them.
        for i, _ in needs:
            deltas[i]["_embedding"] = []
        return 0

    if len(embeddings) != len(needs):
        print(
            f"[resonance] embed returned {len(embeddings)} for {len(needs)} requested — "
            "skipping cache write"
        )
        for i, _ in needs:
            deltas[i]["_embedding"] = []
        return 0

    for (i, _), emb in zip(needs, embeddings):
        deltas[i]["_embedding"] = emb
    return len(embeddings)


async def rank(
    query_text: str,
    candidates: list[dict],
    top_k: int,
) -> list[dict]:
    """Return the top_k candidates by cosine similarity to query_text.

    Embeds the query once, ensures every candidate has an embedding
    (lazy via ensure_embeddings), then sorts descending by score.
    Stable ordering on ties — falls back to candidate input order so
    deterministic recency wins ties.

    Empty query text or empty candidate list returns the candidates
    unchanged (truncated to top_k). Embedding failures degrade to
    input order — the loop never crashes on a resonance miss.
    """
    if not candidates or top_k <= 0:
        return []
    query = (query_text or "").strip()
    if not query:
        return list(candidates[:top_k])

    q_emb = await _embed_query_cached(query)
    if not q_emb:
        return list(candidates[:top_k])

    await ensure_embeddings(candidates)

    scored: list[tuple[int, float, dict]] = []
    for idx, d in enumerate(candidates):
        emb = d.get("_embedding") or []
        score = _cosine(q_emb, emb) if emb else 0.0
        scored.append((idx, score, d))

    # Sort by score desc, then by original index asc (stable on ties).
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [d for _, _, d in scored[:top_k]]
