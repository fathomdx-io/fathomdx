"""Unit tests for api/provenance_centroid.py:compute_centroid.

The centroid is what makes provenance findable by SUBSTANCE — a recall
for the topic of an L2's constituents should surface the L2 itself,
even when the title+summary text doesn't lexically match. Get the
recursion wrong and the centroid drifts (averaging summaries-of-
summaries) or hangs (cycle, runaway depth). Pin both bounds and the
recursive-vs-leaf branch logic.

batch_get is monkeypatched per-test so the lake-of-truth round-trip
stays stubbed; the only thing under test here is the walk + averaging.
"""

from __future__ import annotations

from typing import Any

import pytest

from api import provenance_centroid as pc


def _vec(value: float) -> list[float]:
    """A constant vector — easy to spot in centroid arithmetic."""
    return [value] * pc.VECTOR_DIM


def _base_delta(id_: str, value: float) -> dict[str, Any]:
    """A leaf moment — no kind:provenance tag, embedding present."""
    return {"id": id_, "tags": [], "embedding": _vec(value)}


def _prov_delta(id_: str, from_ids: list[str], value: float = 0.0) -> dict[str, Any]:
    """A kind:provenance delta — `embedding` is title+summary text and
    is NOT used in centroid arithmetic; included to verify the walker
    skips it in favor of recursing through `from:*`."""
    return {
        "id": id_,
        "tags": ["kind:provenance"] + [f"from:{fid}" for fid in from_ids],
        "embedding": _vec(value),
    }


def _qa_delta(id_: str, value: float) -> dict[str, Any]:
    """A kind:qa-marker — provenance-shaped but treated as a leaf
    (a single Q+A pair, no further constituents to walk into)."""
    return {
        "id": id_,
        "tags": ["kind:provenance", "kind:qa-marker"],
        "embedding": _vec(value),
    }


def _patch_lake(monkeypatch: pytest.MonkeyPatch, by_id: dict[str, dict]) -> list[list[str]]:
    """Wire batch_get to read from a fixture dict; record every call so
    tests can assert on call shape (cycle protection, depth limits)."""
    calls: list[list[str]] = []

    async def fake_batch_get(ids: list[str]) -> list[dict]:
        calls.append(list(ids))
        return [by_id[i] for i in ids if i in by_id]

    monkeypatch.setattr(pc.delta_client, "batch_get", fake_batch_get)
    return calls


# ── Empty / degenerate input ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_from_ids_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lake(monkeypatch, {})
    assert await pc.compute_centroid([]) is None
    assert await pc.compute_centroid(None) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_whitespace_and_empty_strings_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-string / empty / whitespace-only ids drop before the lake call;
    if nothing survives the filter, no batch_get fires at all."""
    calls = _patch_lake(monkeypatch, {})
    assert await pc.compute_centroid(["", "  ", None]) is None  # type: ignore[list-item]
    assert calls == []  # no lake call when nothing survived the filter


@pytest.mark.asyncio
async def test_no_leaves_collected_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All ids point to deltas the lake doesn't have → no embeddings
    collected → None. Caller distinguishes this from a successful walk."""
    _patch_lake(monkeypatch, {})
    assert await pc.compute_centroid(["missing-1", "missing-2"]) is None


# ── Single layer (all leaves) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_layer_averages_base_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_id = {
        "a": _base_delta("a", 1.0),
        "b": _base_delta("b", 2.0),
        "c": _base_delta("c", 3.0),
    }
    _patch_lake(monkeypatch, by_id)
    centroid = await pc.compute_centroid(["a", "b", "c"])
    assert centroid is not None
    assert len(centroid) == pc.VECTOR_DIM
    # Mean of 1, 2, 3 = 2.0 across every dimension.
    assert all(abs(v - 2.0) < 1e-9 for v in centroid)


@pytest.mark.asyncio
async def test_qa_marker_treated_as_leaf(monkeypatch: pytest.MonkeyPatch) -> None:
    """kind:qa-marker carries the kind:provenance tag but isn't recursed
    into — it's a single Q+A pair, the walk stops there. Its title+summary
    embedding contributes directly."""
    by_id = {"q": _qa_delta("q", 5.0)}
    _patch_lake(monkeypatch, by_id)
    centroid = await pc.compute_centroid(["q"])
    assert centroid is not None
    assert all(abs(v - 5.0) < 1e-9 for v in centroid)


# ── Recursion — provenance walks down to base moments ──────────────────


@pytest.mark.asyncio
async def test_provenance_recurses_through_from_pointers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L1 → 2 base leaves. The L1's own title+summary embedding (here 99.0,
    a sentinel) must NOT contaminate the centroid — the walker recurses
    through from:* and averages only the leaves."""
    by_id = {
        "l1": _prov_delta("l1", ["leaf_a", "leaf_b"], value=99.0),
        "leaf_a": _base_delta("leaf_a", 1.0),
        "leaf_b": _base_delta("leaf_b", 3.0),
    }
    _patch_lake(monkeypatch, by_id)
    centroid = await pc.compute_centroid(["l1"])
    assert centroid is not None
    # Mean of leaves only: (1 + 3) / 2 = 2.0; the 99.0 sentinel doesn't show.
    assert all(abs(v - 2.0) < 1e-9 for v in centroid)


@pytest.mark.asyncio
async def test_two_level_recursion_walks_to_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2 → L1 → base. The L2's centroid must include base embeddings,
    not L1's title+summary. L1's sentinel is 88.0 to expose any leak."""
    by_id = {
        "l2": _prov_delta("l2", ["l1"], value=77.0),
        "l1": _prov_delta("l1", ["leaf_a", "leaf_b"], value=88.0),
        "leaf_a": _base_delta("leaf_a", 4.0),
        "leaf_b": _base_delta("leaf_b", 6.0),
    }
    _patch_lake(monkeypatch, by_id)
    centroid = await pc.compute_centroid(["l2"])
    assert centroid is not None
    # Only leaves contribute: mean(4, 6) = 5.0.
    assert all(abs(v - 5.0) < 1e-9 for v in centroid)


@pytest.mark.asyncio
async def test_mixed_layer_prov_recurses_base_contributes_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same `from_ids` list mixes a provenance and a base moment. The
    base contributes its own embedding; the provenance recurses to its
    leaves. Both contribute to the same average."""
    by_id = {
        "l1": _prov_delta("l1", ["leaf_a", "leaf_b"]),
        "leaf_a": _base_delta("leaf_a", 1.0),
        "leaf_b": _base_delta("leaf_b", 1.0),
        "direct": _base_delta("direct", 7.0),
    }
    _patch_lake(monkeypatch, by_id)
    centroid = await pc.compute_centroid(["l1", "direct"])
    assert centroid is not None
    # mean(1, 1, 7) = 3.0 — the L1 contributes 2 leaves, direct contributes 1.
    assert all(abs(v - 3.0) < 1e-9 for v in centroid)


# ── Bound discipline — cycle / depth / leaf cap ────────────────────────


@pytest.mark.asyncio
async def test_cycle_does_not_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A → B → A. Without the seen-set the walk would never terminate.
    With it, B's recurse on A finds A already visited and stops."""
    by_id = {
        "a": _prov_delta("a", ["b"]),
        "b": _prov_delta("b", ["a"]),
    }
    calls = _patch_lake(monkeypatch, by_id)
    centroid = await pc.compute_centroid(["a"])
    # No leaves → None, but the important assertion is "didn't infinite-loop":
    assert centroid is None
    # Each id is fetched at most once thanks to the seen-set.
    fetched = [i for ids in calls for i in ids]
    assert fetched.count("a") == 1
    assert fetched.count("b") == 1


@pytest.mark.asyncio
async def test_depth_cap_stops_runaway_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build a 10-deep linear chain L9 → L8 → ... → L0. _MAX_DEPTH=4 caps
    the walk; ids beyond the cap are never fetched."""
    by_id: dict[str, dict] = {}
    for i in range(10):
        next_id = f"L{i - 1}" if i > 0 else "leaf"
        by_id[f"L{i}"] = _prov_delta(f"L{i}", [next_id])
    by_id["leaf"] = _base_delta("leaf", 1.0)

    calls = _patch_lake(monkeypatch, by_id)
    await pc.compute_centroid(["L9"])

    fetched = {i for ids in calls for i in ids}
    # Walk visits L9 (depth 0), L8 (1), L7 (2), L6 (3), L5 (4); at depth 4
    # the `is_provenance and depth < _MAX_DEPTH` branch is False, so L5
    # is treated as a leaf (no embedding present anyway since it's a
    # provenance) and L4 is never fetched.
    for level in range(5, 10):
        assert f"L{level}" in fetched, f"L{level} should have been fetched"
    for level in range(0, 4):
        assert f"L{level}" not in fetched, f"L{level} should NOT have been fetched"


@pytest.mark.asyncio
async def test_leaf_cap_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    """An L1 with more leaves than _LEAF_CAP — collection stops at the cap.
    Tests the early-return inside the d-loop, not just the cap-check at
    function entry."""
    leaf_count = pc._LEAF_CAP + 50
    leaves = [f"leaf_{i}" for i in range(leaf_count)]
    by_id: dict[str, dict] = {
        "l1": _prov_delta("l1", leaves),
    }
    for i, lid in enumerate(leaves):
        by_id[lid] = _base_delta(lid, float(i))

    _patch_lake(monkeypatch, by_id)
    centroid = await pc.compute_centroid(["l1"])
    assert centroid is not None
    # Mean of values 0..299 is 149.5. If the cap leaked, it would be
    # mean(0..349) = 174.5.
    expected = sum(range(pc._LEAF_CAP)) / pc._LEAF_CAP
    assert all(abs(v - expected) < 1e-6 for v in centroid)


# ── Failure tolerance ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_get_failure_returns_what_was_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2 → L1 → base. If the L1 → base fetch raises, the walk logs and
    returns; nothing collected at any leaf → None. Non-fatal: caller
    treats None as 'no centroid this time'."""
    raise_at: set[str] = {"leaf_a", "leaf_b"}

    async def fake(ids: list[str]) -> list[dict]:
        if any(i in raise_at for i in ids):
            raise ConnectionError("simulated lake outage")
        if "l2" in ids:
            return [_prov_delta("l2", ["l1"])]
        if "l1" in ids:
            return [_prov_delta("l1", ["leaf_a", "leaf_b"])]
        return []

    monkeypatch.setattr(pc.delta_client, "batch_get", fake)
    centroid = await pc.compute_centroid(["l2"])
    assert centroid is None  # no leaves collected, but did not raise


@pytest.mark.asyncio
async def test_wrong_dim_embedding_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leaf with a malformed embedding (wrong dim) drops out of the
    average rather than crashing the centroid arithmetic. Pinning this
    so a future model swap that changes VECTOR_DIM doesn't take down
    every centroid recompute."""
    by_id = {
        "good": _base_delta("good", 2.0),
        "wrong_dim": {"id": "wrong_dim", "tags": [], "embedding": [1.0, 2.0]},
        "missing_emb": {"id": "missing_emb", "tags": []},  # no embedding key
    }
    _patch_lake(monkeypatch, by_id)
    centroid = await pc.compute_centroid(["good", "wrong_dim", "missing_emb"])
    assert centroid is not None
    # Only "good" contributes → centroid is its embedding.
    assert all(abs(v - 2.0) < 1e-9 for v in centroid)
