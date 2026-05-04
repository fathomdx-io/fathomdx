"""Compute a provenance's centroid embedding from its constituents.

A provenance has two embeddings, both stored on the same delta row:

  * `embedding` — vector of the title + summary text. Catches META
    queries ("eras", "what topics", "what's been a long arc"). The
    same field every other delta uses; computed by the embed loop.

  * `provenance_embedding` — centroid of constituents' `embedding`
    vectors. Catches SUBSTANTIVE queries that resonate with what the
    provenance is associated with, even when the summary text doesn't
    quite match. The folder-and-its-files analogy: a provenance lives
    in the same neighborhood as the deltas it's associated with.

At search time, a provenance's effective distance becomes
`min(embedding_distance, provenance_embedding_distance)` — credit on
either axis.

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


async def compute_centroid(from_ids: list[str]) -> list[float] | None:
    """Average the embeddings of the deltas listed in `from_ids`.

    Returns the element-wise mean as a list of floats (length
    `VECTOR_DIM`). Returns None if zero constituents resolve to a
    delta with a usable embedding — the caller should treat that as
    "no centroid available, fall back to summary-only retrieval."

    Constituents without embeddings are skipped silently. Old deltas
    or failed-embed entries don't block the centroid; we just compute
    it from whatever's present.
    """
    cleaned = [s for s in (from_ids or []) if isinstance(s, str) and s.strip()]
    if not cleaned:
        return None

    try:
        deltas = await delta_client.batch_get(cleaned)
    except Exception as e:
        log.warning("centroid: batch_get failed: %s: %s", type(e).__name__, e)
        return None

    embeddings: list[list[float]] = []
    for d in deltas or []:
        emb = d.get("embedding") if isinstance(d, dict) else None
        if isinstance(emb, list) and len(emb) == VECTOR_DIM:
            embeddings.append(emb)

    if not embeddings:
        return None

    # Element-wise mean. Pure Python — small N (typically 3-30
    # constituents) keeps this fast enough not to need numpy.
    n = len(embeddings)
    centroid = [0.0] * VECTOR_DIM
    for emb in embeddings:
        for i in range(VECTOR_DIM):
            centroid[i] += emb[i]
    inv = 1.0 / n
    for i in range(VECTOR_DIM):
        centroid[i] *= inv

    return centroid
