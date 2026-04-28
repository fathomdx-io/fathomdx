"""Unit tests for PlanExecutor._apply_noise_rerank.

The plan executor over-fetches by 2x+10 on every search/bridge/chain step
so the noise modifier can demote trash before the limit truncation. Pure-
Python rerank — easy to test, easy to regress. Pin the contract:

  * rerank uses the shared module-level noise centroid (one cache,
    populated lazily on first call)
  * rows without distance are passed through, sorted to the end
  * trims to target_limit after the sort
  * empty input is returned untouched (no centroid call, no crash)
"""

from __future__ import annotations

import deltas.query as query_mod
from deltas.plan import PlanExecutor


def _executor() -> PlanExecutor:
    """PlanExecutor instance with no pool / embedder. _apply_noise_rerank
    doesn't touch either, so None is fine for unit tests."""
    return PlanExecutor(pool=None, embed_fn=None)


def _seed_centroid(centroid: list[float] | None) -> None:
    """Force the module-level cache to a known value so tests don't call
    embed_text(). `None` resets the cache so the next call rebuilds.
    Pass `[]` to simulate the embedder-offline degraded state."""
    query_mod._NOISE_CENTROID_CACHE = centroid


# ── Inputs without distance ────────────────────────────────────────────


def test_empty_input_returned_untouched() -> None:
    _seed_centroid([])  # avoid embed_text on empty path (it short-circuits)
    assert _executor()._apply_noise_rerank([], target_limit=10) == []


def test_rows_without_distance_pass_through() -> None:
    """Filter-step rows (no distance computed) shouldn't crash the rerank;
    they keep their input order and land at the end if any peer has
    distance."""
    _seed_centroid([])
    rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    out = _executor()._apply_noise_rerank(rows, target_limit=10)
    assert [r["id"] for r in out] == ["a", "b", "c"]


# ── Length penalty isolation (centroid disabled) ───────────────────────


def test_short_content_is_penalized() -> None:
    """With the noise centroid empty, only the length term applies.
    Short content (<24 chars) takes a +20% multiplicative bump on
    distance, so a short ack at 0.30 sits behind a long-form delta at
    0.34 even though raw cosine put it ahead."""
    _seed_centroid([])  # disable centroid term, exercise length only
    rows = [
        {"id": "short_ack", "content": "ok", "distance": 0.30, "embedding": []},
        {
            "id": "real_hit",
            "content": "x" * 100,  # past LENGTH_THRESHOLD
            "distance": 0.34,
            "embedding": [],
        },
    ]
    out = _executor()._apply_noise_rerank(rows, target_limit=5)
    # short_ack: 0.30 * 1.20 = 0.36; real_hit: 0.34 * 1.0 = 0.34
    assert [r["id"] for r in out] == ["real_hit", "short_ack"]


def test_at_threshold_not_penalized() -> None:
    """LENGTH_THRESHOLD is a strict lower bound — content of exactly that
    length escapes the bump. Pinned for the same reason the shallow-path
    test pins it: callers tune cutoffs against this boundary."""
    _seed_centroid([])
    rows = [
        {
            "id": "borderline",
            "content": "x" * query_mod.LENGTH_THRESHOLD,
            "distance": 0.5,
            "embedding": [],
        }
    ]
    out = _executor()._apply_noise_rerank(rows, target_limit=5)
    assert out[0]["distance"] == 0.5  # untouched


# ── Centroid penalty isolation (length disabled) ───────────────────────


def test_centroid_aligned_long_content_is_penalized() -> None:
    """A long-form delta whose embedding ALIGNS with the noise centroid
    takes the centroid bump alone (no length penalty for content past
    the threshold). With NOISE = [1, 0] and the row's embedding == NOISE,
    cosine similarity is 1.0, well past NOISE_FLOOR — full NOISE_ALPHA
    bump applies."""
    _seed_centroid([1.0, 0.0])
    rows = [
        {
            "id": "noisy_long",
            "content": "x" * 100,
            "distance": 0.30,
            "embedding": [1.0, 0.0],  # max cosine to noise centroid
        },
        {
            "id": "clean_long",
            "content": "x" * 100,
            "distance": 0.32,
            "embedding": [0.0, 1.0],  # orthogonal — no centroid penalty
        },
    ]
    out = _executor()._apply_noise_rerank(rows, target_limit=5)
    # noisy_long: 0.30 * (1 + NOISE_ALPHA) = 0.30 * 1.35 = 0.405
    # clean_long: 0.32 * 1.0 = 0.32
    assert [r["id"] for r in out] == ["clean_long", "noisy_long"]


def test_orthogonal_embedding_skips_centroid_term() -> None:
    """An embedding orthogonal to the noise centroid (cosine 0) is well
    under NOISE_FLOOR — the centroid term contributes nothing."""
    _seed_centroid([1.0, 0.0])
    rows = [
        {
            "id": "clean",
            "content": "x" * 100,
            "distance": 0.5,
            "embedding": [0.0, 1.0],
        }
    ]
    out = _executor()._apply_noise_rerank(rows, target_limit=5)
    assert out[0]["distance"] == 0.5


# ── Compounding: length + centroid ─────────────────────────────────────


def test_length_and_centroid_compound() -> None:
    """Short content AND noise-aligned embedding stacks both penalties
    multiplicatively — the worst case for trash that pretends to be
    semantically close to the user's query."""
    _seed_centroid([1.0, 0.0])
    rows = [
        {
            "id": "double_trash",
            "content": "ok",
            "distance": 0.20,
            "embedding": [1.0, 0.0],
        },
        {
            "id": "real",
            "content": "x" * 100,
            "distance": 0.35,
            "embedding": [0.0, 1.0],
        },
    ]
    out = _executor()._apply_noise_rerank(rows, target_limit=5)
    # double_trash: 0.20 * 1.20 * 1.35 = 0.324
    # real: 0.35 * 1.0 = 0.35
    assert [r["id"] for r in out] == ["double_trash", "real"]
    # Sanity: double_trash bumped to ~0.324, real untouched at 0.35
    assert abs(out[0]["distance"] - 0.324) < 1e-9
    assert out[1]["distance"] == 0.35


# ── Trim to target_limit ───────────────────────────────────────────────


def test_trims_to_target_limit_after_sort() -> None:
    """The over-fetch (caller's responsibility) drops here — we sort, then
    truncate to the limit the caller actually wanted. Without the rerank
    catching trash, that limit would land back at the noisy SQL ordering."""
    _seed_centroid([1.0, 0.0])
    rows = [
        {
            "id": str(i),
            "content": "x" * 100,
            "distance": 0.10 + 0.01 * i,
            "embedding": [0.0, 1.0],
        }
        for i in range(10)
    ]
    out = _executor()._apply_noise_rerank(rows, target_limit=3)
    assert len(out) == 3
    # Already sorted ascending by SQL — rerank should preserve that since
    # nothing got penalized (orthogonal embeddings, long content).
    assert [r["id"] for r in out] == ["0", "1", "2"]


def test_trim_keeps_post_rerank_winners() -> None:
    """When rerank reorders, trim takes the new top-K — not the SQL top-K.
    This is the whole point of over-fetching: catch a real hit at SQL
    rank 8 that beats a noisy delta at SQL rank 1 after the bump."""
    _seed_centroid([1.0, 0.0])
    rows = [
        # SQL rank 0 — short ack, gets bumped hard
        {"id": "noise", "content": "ok", "distance": 0.10, "embedding": [1.0, 0.0]},
        # SQL rank 1-4 — middling
        {"id": "x1", "content": "x" * 100, "distance": 0.15, "embedding": [0.0, 1.0]},
        {"id": "x2", "content": "x" * 100, "distance": 0.18, "embedding": [0.0, 1.0]},
        {"id": "x3", "content": "x" * 100, "distance": 0.22, "embedding": [0.0, 1.0]},
        {"id": "x4", "content": "x" * 100, "distance": 0.25, "embedding": [0.0, 1.0]},
    ]
    out = _executor()._apply_noise_rerank(rows, target_limit=2)
    # noise: 0.10 * 1.20 * 1.35 = 0.162 (length + centroid both stack)
    # x1: 0.15, x2: 0.18 → top 2 should be x1 then noise (0.162) — but
    # 0.162 > 0.15, so order is x1, noise.
    assert [r["id"] for r in out] == ["x1", "noise"]


# ── Stable sort + None handling ────────────────────────────────────────


def test_distance_none_lands_at_end_with_distanced_peers() -> None:
    """Mixed step (some rows have distance, some don't) — distance-bearing
    rows sort first, None rows tail in their input order."""
    _seed_centroid([])
    rows = [
        {"id": "no_dist_1"},
        {"id": "high", "content": "x" * 100, "distance": 0.5, "embedding": []},
        {"id": "no_dist_2"},
        {"id": "low", "content": "x" * 100, "distance": 0.1, "embedding": []},
    ]
    out = _executor()._apply_noise_rerank(rows, target_limit=10)
    assert [r["id"] for r in out] == ["low", "high", "no_dist_1", "no_dist_2"]


def test_missing_embedding_only_pays_length_cost() -> None:
    """A row carrying distance but no embedding (legacy or interim) just
    pays the length penalty — no crash, no spurious centroid score. This
    is the same soft-fail _noise_modifier promises on the shallow side."""
    _seed_centroid([1.0, 0.0])  # centroid present but row's embedding empty
    rows = [
        {"id": "short_no_emb", "content": "ok", "distance": 0.30, "embedding": []},
        {"id": "long_no_emb", "content": "x" * 100, "distance": 0.34, "embedding": []},
    ]
    out = _executor()._apply_noise_rerank(rows, target_limit=5)
    # short: 0.30 * 1.20 = 0.36
    # long: 0.34 * 1.0 = 0.34
    assert [r["id"] for r in out] == ["long_no_emb", "short_no_emb"]
