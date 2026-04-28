"""Unit tests for query._valence_modifier.

The valence modifier reranks deltas by their engagement cloud at search
time — refuted deltas sink, affirmed / cited ones float. Capped so no
single cloud can nuke or anoint a result.

This file deliberately mirrors api/tests/test_search_rerank.py. The two
services hold parallel implementations of the same scoring (delta-store
applies it on the shallow path, api/search.py applies it post-plan on
the deep path); independent test batteries on each side are how we
catch numerical drift between them without coupling the deployments.
"""

from __future__ import annotations

from deltas.query import VALENCE_MAX_PCT, _valence_modifier


def test_silence_is_neutral() -> None:
    """Empty cloud → 1.0 (no shift). Multiplying distance by 1.0 is a no-op
    so deltas without engagement keep their raw cosine rank."""
    assert _valence_modifier([]) == 1.0


def test_refutes_demotes() -> None:
    """A single refutes:<id> pointer drops the rank — score -1.0 maps to
    shift -0.05, modifier 1.05. Distance increases, delta sinks."""
    cloud = [{"tags": ["refutes:abc"]}]
    assert _valence_modifier(cloud) == 1.05


def test_affirms_promotes() -> None:
    """A single affirms:<id> raises rank — score 1.0 maps to shift 0.05,
    modifier 0.95. Distance decreases, delta surfaces."""
    cloud = [{"tags": ["affirms:abc"]}]
    assert _valence_modifier(cloud) == 0.95


def test_from_provenance_is_implicit_half_affirm() -> None:
    """Sediment citing a source via from:<id> is implicit positive
    engagement — half the lift of an explicit affirm. Ensures provenance
    deltas float without being treated as user-volunteered approval."""
    cloud = [{"tags": ["from:abc"]}]
    # score 0.5 → shift 0.025 → modifier 0.975
    assert _valence_modifier(cloud) == 0.975


def test_engages_and_replyto_are_quarter_lift() -> None:
    """Neutral attention pointers (engages, reply-to) get a quarter of
    an affirm's lift — they signal the delta got noticed, not endorsed."""
    cloud = [{"tags": ["engages:abc"]}]
    assert _valence_modifier(cloud) == 1.0 - 0.0125


def test_pointer_break_picks_first_match_only() -> None:
    """Each cloud member contributes once for its pointer-type — the
    inner for-tag loop breaks on first prefix match. A member tagged
    BOTH refutes:x AND affirms:y counts as the FIRST one in iteration
    order. Pinned: silently swapping this for "sum all matching prefixes"
    would change the math."""
    cloud = [{"tags": ["refutes:x", "affirms:y"]}]
    assert _valence_modifier(cloud) == 1.05  # refutes first → -1
    cloud = [{"tags": ["affirms:y", "refutes:x"]}]
    assert _valence_modifier(cloud) == 0.95  # affirms first → +1


def test_engagement_more_less_stack_with_pointer() -> None:
    """`engagement:more` and `engagement:less` are checked AFTER the
    inner break-loop, so they stack with whichever pointer prefix won.
    A delta the user thumb'd up AND a sediment cited adds 1.0 (affirm)
    + 0.5 (engagement:more) = 1.5 score → 0.075 shift → 0.925 modifier."""
    cloud = [{"tags": ["affirms:x", "engagement:more"]}]
    assert _valence_modifier(cloud) == 0.925
    cloud = [{"tags": ["affirms:x", "engagement:less"]}]
    assert _valence_modifier(cloud) == 0.975


def test_score_accumulates_across_members() -> None:
    """Cloud members contribute additively. -1 + 1 + 0.5 = 0.5 → shift
    0.025 → modifier 0.975."""
    cloud = [
        {"tags": ["refutes:a"]},
        {"tags": ["affirms:b"]},
        {"tags": ["from:c"]},
    ]
    assert _valence_modifier(cloud) == 0.975


def test_caps_at_max_pct_either_direction() -> None:
    """A pile of affirms can't drive the modifier below 1 - VALENCE_MAX_PCT;
    a pile of refutes can't drive it above 1 + VALENCE_MAX_PCT. Stops a
    single rabidly-engaged delta from dominating the trail."""
    big_affirm = [{"tags": [f"affirms:{i}"]} for i in range(100)]
    big_refute = [{"tags": [f"refutes:{i}"]} for i in range(100)]
    assert _valence_modifier(big_affirm) == 1.0 - VALENCE_MAX_PCT
    assert _valence_modifier(big_refute) == 1.0 + VALENCE_MAX_PCT


def test_missing_or_none_tags_skipped() -> None:
    """Cloud member without tags (legacy or malformed row) contributes
    nothing — the score loop reads `(d.get("tags") or [])` defensively."""
    assert _valence_modifier([{"tags": None}]) == 1.0
    assert _valence_modifier([{}]) == 1.0


def test_max_pct_constant_matches_api_side() -> None:
    """The api/search.py mirror uses _VALENCE_MAX_PCT = 0.30. If this
    side bumps the cap, the doc on the other side becomes wrong silently.
    Pinning the value here flags any drift at test-collection time."""
    assert VALENCE_MAX_PCT == 0.30
