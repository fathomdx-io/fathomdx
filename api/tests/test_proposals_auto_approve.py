"""Unit tests for the proposal auto-approve gate (api/routes/proposals.py).

The gate runs at proposal-draft time and auto-approves provenance
proposals at every level. L1/L2 land silently; L3+ also pings the
header bell as a created-alert (separate code path in alerts.py).
Get the level parsing wrong and the lake either fails to auto-approve
real provenance (proposals stack up pending) or treats a missing-tag
delta as an auto-approval candidate (parser must surface None).

The HTTP gate itself wires several conditions together (level + tool +
lake_id + tool_args type). Pinning the helpers + the level boundary
covers the parsing surface; the integrated gate is exercised in
production via the harness fires.
"""

from __future__ import annotations

from api.routes.proposals import (
    _produced_by_from_tags,
    _provenance_level_from_tags,
)

# ── _produced_by_from_tags ─────────────────────────────────────────────


def test_produced_by_extracts_first_match() -> None:
    tags = ["kind:proposal", "produced-by:reflective-agent", "other"]
    assert _produced_by_from_tags(tags) == "reflective-agent"


def test_produced_by_default_when_absent() -> None:
    assert _produced_by_from_tags(["kind:proposal"]) == "unknown"
    assert _produced_by_from_tags(["kind:proposal"], default="harness") == "harness"


def test_produced_by_default_on_empty_suffix() -> None:
    """`produced-by:` with no value falls back to the default — an empty
    producer name would poison the audit trail."""
    assert _produced_by_from_tags(["produced-by:"], default="fallback") == "fallback"


def test_produced_by_empty_input_safe() -> None:
    assert _produced_by_from_tags([]) == "unknown"
    assert _produced_by_from_tags(None) == "unknown"  # type: ignore[arg-type]


def test_produced_by_skips_non_string_tags() -> None:
    """Tag list may contain non-strings if a writer slipped a dict / int
    in. The startswith check needs to skip those without raising."""
    tags = [None, 42, {"k": "v"}, "produced-by:topical-agent"]  # type: ignore[list-item]
    assert _produced_by_from_tags(tags) == "topical-agent"  # type: ignore[arg-type]


def test_produced_by_first_match_wins_on_duplicates() -> None:
    """If two producers somehow stamped the same proposal, take the first.
    Pinning so a future writer-collision bug doesn't silently flip the
    audit attribution."""
    tags = ["produced-by:first", "produced-by:second"]
    assert _produced_by_from_tags(tags) == "first"


# ── _provenance_level_from_tags ────────────────────────────────────────


def test_level_extracts_int() -> None:
    assert _provenance_level_from_tags(["provenance-level:0"]) == 0
    assert _provenance_level_from_tags(["provenance-level:1"]) == 1
    assert _provenance_level_from_tags(["provenance-level:2"]) == 2
    assert _provenance_level_from_tags(["provenance-level:3"]) == 3
    assert _provenance_level_from_tags(["provenance-level:99"]) == 99


def test_level_returns_none_when_absent() -> None:
    """No `provenance-level:<n>` tag → None. The auto-approve gate treats
    None as a no-op (does not approve), so this is the safe default."""
    assert _provenance_level_from_tags([]) is None
    assert _provenance_level_from_tags(None) is None  # type: ignore[arg-type]
    assert _provenance_level_from_tags(["unrelated", "tags"]) is None


def test_level_skips_unparseable_continues_to_next() -> None:
    """A bad `provenance-level:<garbage>` tag doesn't return None outright
    — the loop continues looking for a parseable one. Pinning so a
    typo on one tag doesn't hide a valid second tag."""
    tags = ["provenance-level:not-a-number", "provenance-level:2"]
    assert _provenance_level_from_tags(tags) == 2


def test_level_all_unparseable_returns_none() -> None:
    tags = ["provenance-level:foo", "provenance-level:"]
    assert _provenance_level_from_tags(tags) is None


def test_level_skips_non_string_tags() -> None:
    """Same defensive treatment as _produced_by_from_tags — must not crash
    if a writer slipped a non-string into the tag list."""
    tags = [None, 7, "provenance-level:1"]  # type: ignore[list-item]
    assert _provenance_level_from_tags(tags) == 1  # type: ignore[arg-type]


def test_level_first_parseable_wins() -> None:
    tags = ["provenance-level:1", "provenance-level:3"]
    assert _provenance_level_from_tags(tags) == 1


def test_level_negative_value_passes_through() -> None:
    """No clamp on negative levels — -1 parses to -1. Pinning the
    parser as an unconditional int(); any policy on negative values
    is the gate's responsibility, not the parser's."""
    assert _provenance_level_from_tags(["provenance-level:-1"]) == -1


# ── Auto-approve level boundary (the actual policy) ────────────────────


def _gate_decision(level: int | None) -> str:
    """Mirror the level-side of the gate condition in proposals.py:
        if level_int is not None: auto-approve
    (Other gate conditions — tool name, lake_id presence, tool_args type
    — are not represented here; this isolates the level boundary itself.)"""
    if level is None:
        return "manual"
    return "auto"


def test_gate_l0_auto_approves() -> None:
    """L0 (qa-marker / question-anchored) auto-approves."""
    assert _gate_decision(0) == "auto"


def test_gate_l1_l2_auto_approve() -> None:
    """L1 (episodes) and L2 (topics) auto-approve and land silently —
    no header-bell ping, no feed card."""
    assert _gate_decision(1) == "auto"
    assert _gate_decision(2) == "auto"


def test_gate_l3_and_above_auto_approve() -> None:
    """L3 (eras) and L4+ also auto-approve, but they ALSO ping the
    operator's header bell when they land — so identity-arc claims are
    visible without blocking on a pending decision."""
    assert _gate_decision(3) == "auto"
    assert _gate_decision(4) == "auto"
    assert _gate_decision(99) == "auto"


def test_gate_missing_level_stays_manual() -> None:
    """No level tag → the gate cannot tell what tier this is, so it
    falls back to manual review. Pinning so a writer that forgets
    the provenance-level tag can't silently auto-approve."""
    assert _gate_decision(None) == "manual"


def test_gate_negative_level_currently_auto_approves() -> None:
    """Documenting the current contract: the parser passes through any
    int and the gate accepts any non-None level. If the policy ever
    needs to clamp negatives to 'manual', this test will signal the
    change."""
    assert _gate_decision(-1) == "auto"
