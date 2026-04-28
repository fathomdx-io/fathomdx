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

import math

from .. import delta_client


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

    try:
        q_embs = await delta_client.embed([query[:1000]])
    except Exception as e:
        print(f"[resonance] query embed failed: {type(e).__name__}: {e}")
        return list(candidates[:top_k])
    if not q_embs or not q_embs[0]:
        return list(candidates[:top_k])
    q_emb = q_embs[0]

    await ensure_embeddings(candidates)

    scored: list[tuple[int, float, dict]] = []
    for idx, d in enumerate(candidates):
        emb = d.get("_embedding") or []
        score = _cosine(q_emb, emb) if emb else 0.0
        scored.append((idx, score, d))

    # Sort by score desc, then by original index asc (stable on ties).
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [d for _, _, d in scored[:top_k]]
