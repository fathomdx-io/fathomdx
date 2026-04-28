"""Unit tests for api/search.py rerank + provenance helpers.

Pure-Python helpers that run on every deep recall after the plan executor
returns. They mutate the deltas-by-step dict in place. A regression here
silently re-orders results in production — no test framework would catch
it without these.
"""

from __future__ import annotations

from api.search import (
    _SEDIMENT_PROVENANCE_LIMIT,
    _VALENCE_MAX_PCT,
    _apply_valence_rerank,
    _provenance_ids_from_deltas,
    _valence_modifier,
    _valence_score,
)

# ── _valence_score ─────────────────────────────────────────────────────


def test_empty_cloud_scores_zero() -> None:
    assert _valence_score([]) == 0.0
    assert _valence_score([{"tags": []}]) == 0.0
    assert _valence_score([{"tags": None}]) == 0.0


def test_refutes_pulls_score_negative() -> None:
    cloud = [{"tags": ["refutes:abc123"]}]
    assert _valence_score(cloud) == -1.0


def test_affirms_pulls_score_positive() -> None:
    cloud = [{"tags": ["affirms:abc123"]}]
    assert _valence_score(cloud) == 1.0


def test_from_provenance_is_implicit_half_affirm() -> None:
    cloud = [{"tags": ["from:abc123"]}]
    assert _valence_score(cloud) == 0.5


def test_engages_and_replyto_are_quarter_affirm() -> None:
    assert _valence_score([{"tags": ["engages:abc"]}]) == 0.25
    assert _valence_score([{"tags": ["reply-to:abc"]}]) == 0.25


def test_pointer_break_picks_first_match_only() -> None:
    """Each cloud member contributes once for its pointer-type — the inner
    loop breaks on first prefix match. This pins behavior: a member tagged
    BOTH refutes:x AND affirms:y counts as -1 (whichever appears first),
    not as 0. Same as delta-store's _valence_modifier."""
    cloud = [{"tags": ["refutes:x", "affirms:y"]}]
    assert _valence_score(cloud) == -1.0
    cloud = [{"tags": ["affirms:y", "refutes:x"]}]
    assert _valence_score(cloud) == 1.0


def test_engagement_more_less_stack_with_pointer() -> None:
    """The pointer-type signal (refutes/affirms/from/engages/reply-to)
    breaks out of the for-tag loop, but `engagement:more` and
    `engagement:less` are checked AFTER the loop and stack additively."""
    cloud = [{"tags": ["affirms:x", "engagement:more"]}]
    assert _valence_score(cloud) == 1.5
    cloud = [{"tags": ["affirms:x", "engagement:less"]}]
    assert _valence_score(cloud) == 0.5


def test_score_accumulates_across_members() -> None:
    cloud = [
        {"tags": ["refutes:a"]},
        {"tags": ["affirms:b"]},
        {"tags": ["from:c"]},
    ]
    # -1 + 1 + 0.5
    assert _valence_score(cloud) == 0.5


# ── _valence_modifier ──────────────────────────────────────────────────


def test_modifier_silence_is_neutral() -> None:
    """No cloud → 1.0 (no shift). Multiplying distance by 1.0 is a no-op."""
    assert _valence_modifier([]) == 1.0


def test_modifier_under_1_when_score_positive() -> None:
    """affirms drops distance — score 1.0 maps to shift 0.05, modifier 0.95."""
    cloud = [{"tags": ["affirms:x"]}]
    assert _valence_modifier(cloud) == 0.95


def test_modifier_over_1_when_score_negative() -> None:
    """refutes raises distance — score -1.0 maps to shift -0.05, modifier 1.05."""
    cloud = [{"tags": ["refutes:x"]}]
    assert _valence_modifier(cloud) == 1.05


def test_modifier_caps_at_max_pct_either_direction() -> None:
    """A pile of affirms can't drive the modifier below 1 - VALENCE_MAX_PCT;
    a pile of refutes can't drive it above 1 + VALENCE_MAX_PCT. This stops
    a single rabidly-engaged delta from nuking or anointing the trail."""
    big_affirm = [{"tags": [f"affirms:{i}"]} for i in range(100)]
    big_refute = [{"tags": [f"refutes:{i}"]} for i in range(100)]
    assert _valence_modifier(big_affirm) == 1.0 - _VALENCE_MAX_PCT
    assert _valence_modifier(big_refute) == 1.0 + _VALENCE_MAX_PCT


# ── _apply_valence_rerank ──────────────────────────────────────────────


def _delta(id_: str, distance: float | None, cloud: list[dict] | None = None) -> dict:
    """Test factory for a delta dict with optional distance + cloud."""
    d: dict = {"id": id_}
    if distance is not None:
        d["distance"] = distance
    if cloud is not None:
        d["engagement_cloud"] = cloud
    return d


def test_rerank_no_clouds_is_noop() -> None:
    """Distances unchanged when no engagement clouds are attached. Order
    is preserved — when no delta in the step has both distance and cloud,
    `any_distance` stays False and the sort never fires."""
    deltas = [
        _delta("a", 0.1),
        _delta("b", 0.5),
        _delta("c", 0.3),
    ]
    deltas_by_step = {"s1": deltas}
    _apply_valence_rerank(deltas_by_step)
    assert [d["id"] for d in deltas] == ["a", "b", "c"]
    assert [d["distance"] for d in deltas] == [0.1, 0.5, 0.3]


def test_rerank_floats_affirmed_above_neutral() -> None:
    """A neutral delta at 0.20 and an affirmed delta at 0.21 — without
    rerank, neutral wins. With rerank, 0.21 * 0.95 = 0.1995 < 0.20, so
    affirmed surfaces first."""
    deltas = [
        _delta("neutral", 0.20),
        _delta("affirmed", 0.21, [{"tags": ["affirms:x"]}]),
    ]
    deltas_by_step = {"s1": deltas}
    _apply_valence_rerank(deltas_by_step)
    assert [d["id"] for d in deltas] == ["affirmed", "neutral"]


def test_rerank_sinks_refuted_below_neutral() -> None:
    """A refuted delta at 0.20 and a neutral delta at 0.21 — without
    rerank, refuted wins. With rerank, 0.20 * 1.05 = 0.21, tied; the
    sort is stable so input order breaks the tie."""
    deltas = [
        _delta("refuted", 0.20, [{"tags": ["refutes:x"]}]),
        _delta("neutral", 0.205),
    ]
    deltas_by_step = {"s1": deltas}
    _apply_valence_rerank(deltas_by_step)
    # refuted 0.20 * 1.05 = 0.21, > neutral 0.205 → neutral first
    assert [d["id"] for d in deltas] == ["neutral", "refuted"]


def test_rerank_skips_deltas_without_distance() -> None:
    """Filter / aggregate steps don't carry distance back from the executor.
    Those rows must not crash the rerank — they keep their input order
    relative to one another and land at the end of any step that ALSO
    has distance-bearing rows."""
    deltas = [
        _delta("a", None, [{"tags": ["affirms:x"]}]),  # distance None — skipped
        _delta("b", 0.5),
        _delta("c", 0.4, [{"tags": ["refutes:x"]}]),  # 0.4 * 1.05 = 0.42
    ]
    deltas_by_step = {"s1": deltas}
    _apply_valence_rerank(deltas_by_step)
    # b (0.5) and c (0.42) reranked; a stays at end.
    assert [d["id"] for d in deltas] == ["c", "b", "a"]


def test_rerank_zero_distance_is_immutable() -> None:
    """A perfect-match delta (distance 0.0) can't be refuted off the top
    of the list because 0.0 * anything = 0.0. This is intentional — the
    valence layer is a tiebreaker, not a veto."""
    deltas = [
        _delta("perfect", 0.0, [{"tags": ["refutes:x"]}]),
        _delta("close", 0.01),
    ]
    deltas_by_step = {"s1": deltas}
    _apply_valence_rerank(deltas_by_step)
    assert [d["id"] for d in deltas] == ["perfect", "close"]
    assert deltas[0]["distance"] == 0.0


def test_rerank_iterates_all_steps_independently() -> None:
    """Each step's deltas list sorts independently. A refute in one step
    doesn't bleed into another step's order."""
    s1 = [_delta("a", 0.2, [{"tags": ["refutes:x"]}]), _delta("b", 0.21)]
    s2 = [_delta("c", 0.5), _delta("d", 0.4)]
    deltas_by_step = {"s1": s1, "s2": s2}
    _apply_valence_rerank(deltas_by_step)
    assert [d["id"] for d in s1] == ["b", "a"]  # refute lifted a to back
    # s2 had no clouds — any_distance stayed False, no sort fired
    assert [d["id"] for d in s2] == ["c", "d"]


# ── _provenance_ids_from_deltas ────────────────────────────────────────


def test_provenance_ignores_non_sediment() -> None:
    """Only kind:sediment deltas contribute from: pointers. A regular
    delta with from:<id> in its tags (e.g. a hand-tagged note) is
    ignored — the auto-expand is sediment-specific by design."""
    deltas = [
        {"tags": ["from:abc"]},  # not sediment — skipped
        {"tags": ["routine-fire", "from:def"]},  # not sediment — skipped
    ]
    assert _provenance_ids_from_deltas(deltas, set()) == []


def test_provenance_extracts_from_pointers() -> None:
    deltas = [
        {"tags": ["kind:sediment", "from:abc", "from:def"]},
    ]
    assert _provenance_ids_from_deltas(deltas, set()) == ["abc", "def"]


def test_provenance_drops_already_seen() -> None:
    """The cited source set may include ids that already surfaced
    organically in earlier steps — those are excluded so the synthetic
    _provenance step doesn't double-render them."""
    deltas = [{"tags": ["kind:sediment", "from:abc", "from:def"]}]
    assert _provenance_ids_from_deltas(deltas, {"abc"}) == ["def"]


def test_provenance_dedupes_across_sediments() -> None:
    """Two sediments citing the same source — the source surfaces once."""
    deltas = [
        {"tags": ["kind:sediment", "from:shared", "from:a"]},
        {"tags": ["kind:sediment", "from:shared", "from:b"]},
    ]
    # First-seen order: shared, a, b (b comes after shared on second sediment
    # but shared is dedup'd).
    assert _provenance_ids_from_deltas(deltas, set()) == ["shared", "a", "b"]


def test_provenance_preserves_first_seen_order() -> None:
    deltas = [
        {"tags": ["kind:sediment", "from:third", "from:first"]},
        {"tags": ["kind:sediment", "from:second"]},
    ]
    assert _provenance_ids_from_deltas(deltas, set()) == ["third", "first", "second"]


def test_provenance_strips_whitespace_in_pointer() -> None:
    """`from:<id>` strips whitespace from the id — defensive for hand-
    crafted tags or pasted content."""
    deltas = [{"tags": ["kind:sediment", "from:  abc  "]}]
    assert _provenance_ids_from_deltas(deltas, set()) == ["abc"]


def test_provenance_skips_empty_pointer() -> None:
    """`from:` with no id (empty after strip) is skipped — a malformed
    sediment shouldn't crash the auto-expand."""
    deltas = [{"tags": ["kind:sediment", "from:", "from:   "]}]
    assert _provenance_ids_from_deltas(deltas, set()) == []


def test_provenance_limit_is_a_real_constant() -> None:
    """The cap on auto-expanded ids is a public-ish constant the caller
    can lower without re-grepping for a magic number. Pin it so a future
    rename surfaces here."""
    assert isinstance(_SEDIMENT_PROVENANCE_LIMIT, int)
    assert _SEDIMENT_PROVENANCE_LIMIT > 0
