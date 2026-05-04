"""Compute a provenance's centroid embedding from its constituents.

A provenance has two embeddings, both stored on the same delta row:

  * `embedding` — vector of the title + summary text. Catches META
    queries ("eras", "what topics", "what's been a long arc"). The
    same field every other delta uses; computed by the embed loop.

  * `provenance_embedding` — centroid of constituents' `embedding`
    vectors, walked recursively to base moments. Catches SUBSTANTIVE
    queries that resonate with what the provenance is associated
    with. The folder-and-its-files analogy: a provenance lives in
    the same neighborhood as the deltas it's associated with.

At search time, a provenance's effective distance becomes
`min(embedding_distance, provenance_embedding_distance)` — credit on
either axis.

The recursive walk: when a constituent is itself a `kind:provenance`
delta, we DON'T just use its centroid (which is itself an average,
already lossy). Instead we walk down through `from:*` chains to
collect the base moments at the leaves, then average those. This
keeps the centroid grounded in primary material rather than chains
of summaries-of-summaries.

Append-only respected: stale centroids on old provenance are fine.
We never recompute existing provenance centroids automatically.
Newer activity in the same area produces newer, sharper provenance
that supersedes the old via search ranking, without touching the
historical strata.

The legacy `provenance_embedding` for non-provenance deltas is the
embedding of the joined tag string (set by delta-store's background
embed loop for every delta). We keep that for legacy 3D-search
behavior; the centroid override applies ONLY to `kind:provenance`
deltas. The embed loop is gated to skip overwriting
`provenance_embedding` when the delta is `kind:provenance`.
"""

from __future__ import annotations

import logging

from . import delta_client

log = logging.getLogger(__name__)


VECTOR_DIM = 512

# Recursion controls for centroid descent. Most provenance trees are
# shallow (L3 → L2 → L1 → base = depth 3). Cap at 4 so even a future
# L4-shaped hierarchy works, but pathological cycles can't blow up
# the walk.
_MAX_DEPTH = 4
# Hard cap on base-moment embeddings collected for one centroid.
# An L3 era covering ~10 L2s × ~10 L1s × ~15 base = 1500 leaves; we
# subsample to keep the average tractable and avoid overweighting
# bloated branches. 300 is generous given the 5-30 per-node guideline
# and gives a stable mean even on uneven sub-trees.
_LEAF_CAP = 300


async def compute_centroid(from_ids: list[str]) -> list[float] | None:
    """Recursively walk `from_ids` down to base moments, average their
    embeddings, return the element-wise mean.

    For each id in `from_ids`:
      * if the delta is a base moment (no `kind:provenance` tag) or a
        `kind:qa-marker`: use its embedding directly.
      * if the delta is a `kind:provenance`: recurse through ITS
        `from:*` tags, collecting base-moment embeddings at the
        leaves. Avoids using already-averaged provenance centroids
        as input to a new centroid (averaging-of-averages dilutes).

    Returns None if zero leaves are reached.

    Capped at `_MAX_DEPTH` levels and `_LEAF_CAP` total embeddings to
    keep the walk bounded — a runaway hierarchy or cyclic from:* tag
    can't tank latency.
    """
    cleaned = [s for s in (from_ids or []) if isinstance(s, str) and s.strip()]
    if not cleaned:
        return None

    seen: set[str] = set()
    embeddings: list[list[float]] = []

    async def collect(ids: list[str], depth: int) -> None:
        if depth > _MAX_DEPTH or len(embeddings) >= _LEAF_CAP:
            return
        # Filter ids we've already visited (cycle / DAG protection).
        fresh = [i for i in ids if i and i not in seen]
        if not fresh:
            return
        for i in fresh:
            seen.add(i)
        try:
            deltas = await delta_client.batch_get(fresh)
        except Exception as e:
            log.warning(
                "centroid: batch_get failed at depth %d: %s: %s", depth, type(e).__name__, e
            )
            return

        next_layer: list[str] = []
        for d in deltas or []:
            if not isinstance(d, dict):
                continue
            tags = d.get("tags") or []
            is_provenance = "kind:provenance" in tags and "kind:qa-marker" not in tags
            if is_provenance and depth < _MAX_DEPTH:
                # Walk down — collect this provenance's constituents
                # rather than using its (already-averaged) centroid.
                child_ids = [
                    t.split(":", 1)[1] for t in tags if isinstance(t, str) and t.startswith("from:")
                ]
                next_layer.extend(child_ids)
            else:
                # Base moment (or qa-marker, which is a single Q+A pair
                # treated as a leaf). Use its embedding if present.
                emb = d.get("embedding")
                if isinstance(emb, list) and len(emb) == VECTOR_DIM:
                    embeddings.append(emb)
                    if len(embeddings) >= _LEAF_CAP:
                        return

        if next_layer:
            await collect(next_layer, depth + 1)

    await collect(cleaned, 0)

    if not embeddings:
        return None

    # Element-wise mean. Pure Python — bounded by _LEAF_CAP so this
    # stays fast enough not to need numpy for prov-scale lakes.
    n = len(embeddings)
    centroid = [0.0] * VECTOR_DIM
    for emb in embeddings:
        for i in range(VECTOR_DIM):
            centroid[i] += emb[i]
    inv = 1.0 / n
    for i in range(VECTOR_DIM):
        centroid[i] *= inv

    return centroid
