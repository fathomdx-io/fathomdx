"""Unit tests for the recall abstain gate (api/search.py).

Vector search never abstains on its own — with nothing genuinely close,
it still returns its least-far-away noise, and the provenance/timeline
expansions then decorate that noise into a wall that reads as relevant.
The gate gives recall permission to return empty: when no ``search``-step
anchor clears the distance floor, the whole recall bails before any
expansion or sediment write runs.

Two surfaces under test:

  * ``_min_anchor_distance`` — pure helper: which steps count as
    semantic anchors, and what "no evidence" returns.
  * ``_build_result_from_plan_response`` — the gate placement: abstains
    before the expansion layers, never fires unarmed, never fires on
    absent evidence.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import api.search as search_mod
from api.search import _build_result_from_plan_response, _min_anchor_distance

# ── _min_anchor_distance ───────────────────────────────────────────────


def test_picks_min_across_search_steps() -> None:
    plan = {"steps": [{"id": "a", "search": "x"}, {"id": "b", "search": "y"}]}
    deltas = {
        "a": [{"id": "1", "distance": 0.41}, {"id": "2", "distance": 0.38}],
        "b": [{"id": "3", "distance": 0.29}],
    }
    assert _min_anchor_distance(plan, deltas) == 0.29


def test_non_search_steps_never_count_as_anchors() -> None:
    """bridge/chain reach sideways by design (their hits SHOULD be far);
    neighbors fakes distance as a temporal gap (delta-store plan.py) —
    a near-zero "distance" there is a same-second neighbor, not a
    semantic match. Neither may vouch for topical relevance."""
    plan = {
        "steps": [
            {"id": "a", "search": "x"},
            {"id": "b", "chain": "a"},
            {"id": "c", "bridge": ["a", "b"]},
            {"id": "d", "neighbors": "a"},
        ]
    }
    deltas = {
        "a": [{"id": "1", "distance": 0.9}],  # far anchor
        "b": [{"id": "2", "distance": 0.05}],  # close chain hit — ignored
        "c": [{"id": "3", "distance": 0.05}],  # close bridge hit — ignored
        "d": [{"id": "4", "distance": 0.01}],  # temporal gap — ignored
    }
    assert _min_anchor_distance(plan, deltas) == 0.9


def test_none_when_no_search_step() -> None:
    """A filter-only plan carries no semantic anchor — there is no
    evidence either way, so the helper must return None (and the gate
    must not abstain on it)."""
    plan = {"steps": [{"id": "a", "filter": {"tags": ["kind:mood"]}}]}
    deltas = {"a": [{"id": "1"}]}
    assert _min_anchor_distance(plan, deltas) is None


def test_none_when_search_hits_carry_no_distance() -> None:
    plan = {"steps": [{"id": "a", "search": "x"}]}
    deltas = {"a": [{"id": "1"}, {"id": "2", "distance": None}]}
    assert _min_anchor_distance(plan, deltas) is None


def test_missing_and_empty_steps_tolerated() -> None:
    plan = {"steps": [{"id": "a", "search": "x"}, {"id": "b", "search": "y"}]}
    assert _min_anchor_distance(plan, {}) is None
    assert _min_anchor_distance(plan, {"a": []}) is None
    assert _min_anchor_distance({}, {}) is None


# ── gate integration ───────────────────────────────────────────────────
#
# _build_result_from_plan_response calls three async expansion layers
# that talk to the delta store. The no-op patches below stand in for
# them; the abstain-path test additionally asserts they were never
# reached — that placement (gate BEFORE expansions) is the point: an
# abstained recall must not bloom provenance or write sediment.


def _patch_expansions(monkeypatch) -> dict[str, AsyncMock]:
    mocks = {
        "_expand_sediment_provenance": AsyncMock(),
        "_expand_upward_to_provenance": AsyncMock(),
        "_attach_engagement_clouds": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(search_mod, name, mock)
    return mocks


def _plan_response(hits: list[dict]) -> tuple[dict, dict]:
    plan = {"steps": [{"id": "root", "search": "q", "relation": "surfaced"}]}
    response = {"steps": {"root": {"deltas": hits}}}
    return plan, response


async def test_abstains_when_no_anchor_clears_floor(monkeypatch) -> None:
    mocks = _patch_expansions(monkeypatch)
    plan, response = _plan_response(
        [{"id": "1", "distance": 0.52}, {"id": "2", "distance": 0.61}]
    )
    result = await _build_result_from_plan_response(
        text="q", plan=plan, response=response, view="deltas",
        do_sediment=False, threshold=0.35,
    )
    assert result["total_count"] == 0
    assert result["deltas_by_step"] == {}
    assert result["as_prompt"] == ""
    assert result["plan"] == plan  # plan preserved for callers/logging
    for name, mock in mocks.items():
        assert not mock.called, f"{name} ran on an abstained recall"


async def test_passes_when_one_anchor_clears_floor(monkeypatch) -> None:
    """One genuine hit lets the whole trail through — including far hits
    in the same step. Per-hit trimming is deliberately NOT this gate's
    job; it judges the recall as a whole."""
    _patch_expansions(monkeypatch)
    plan, response = _plan_response(
        [{"id": "1", "distance": 0.20}, {"id": "2", "distance": 0.61}]
    )
    result = await _build_result_from_plan_response(
        text="q", plan=plan, response=response, view="deltas",
        do_sediment=False, threshold=0.35,
    )
    assert result["total_count"] == 2
    assert [d["id"] for d in result["deltas_by_step"]["root"]] == ["1", "2"]


async def test_boundary_hit_at_exactly_floor_passes(monkeypatch) -> None:
    """The floor is inclusive (<=), matching the legacy shallow filter
    ``distance <= threshold`` so the two paths never disagree at the
    boundary."""
    _patch_expansions(monkeypatch)
    plan, response = _plan_response([{"id": "1", "distance": 0.35}])
    result = await _build_result_from_plan_response(
        text="q", plan=plan, response=response, view="deltas",
        do_sediment=False, threshold=0.35,
    )
    assert result["total_count"] == 1


async def test_no_threshold_means_no_gate(monkeypatch) -> None:
    """threshold=None is the unarmed state — existing callers that never
    send a threshold keep exactly the old behavior."""
    _patch_expansions(monkeypatch)
    plan, response = _plan_response([{"id": "1", "distance": 0.99}])
    result = await _build_result_from_plan_response(
        text="q", plan=plan, response=response, view="deltas",
        do_sediment=False, threshold=None,
    )
    assert result["total_count"] == 1


async def test_filter_only_plan_never_abstains(monkeypatch) -> None:
    """No search step → no semantic evidence → no abstention. A tag
    filter that matched real deltas must surface them even with the
    gate armed."""
    _patch_expansions(monkeypatch)
    plan = {"steps": [{"id": "a", "filter": {"tags": ["kind:mood"]}}]}
    response = {"steps": {"a": {"deltas": [{"id": "1"}]}}}
    result = await _build_result_from_plan_response(
        text="q", plan=plan, response=response, view="deltas",
        do_sediment=False, threshold=0.35,
    )
    assert result["total_count"] == 1


async def test_far_search_plus_close_chain_still_abstains(monkeypatch) -> None:
    """A chain hit at 0.05 is close to the SEED's centroid, not to the
    query — it cannot rescue a recall whose anchor missed. This is the
    case where decoration used to dress noise up as memory."""
    mocks = _patch_expansions(monkeypatch)
    plan = {
        "steps": [
            {"id": "a", "search": "q"},
            {"id": "b", "chain": "a"},
        ]
    }
    response = {
        "steps": {
            "a": {"deltas": [{"id": "1", "distance": 0.70}]},
            "b": {"deltas": [{"id": "2", "distance": 0.05}]},
        }
    }
    result = await _build_result_from_plan_response(
        text="q", plan=plan, response=response, view="deltas",
        do_sediment=False, threshold=0.35,
    )
    assert result["total_count"] == 0
    assert not mocks["_expand_upward_to_provenance"].called
