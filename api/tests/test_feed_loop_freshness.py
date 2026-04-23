"""Unit tests for the map-based freshness predicate.

These pin the semantics that replaced the N-round-trip per-line
`_has_fresh_card` in the feed loop. If the map path drifts from the
per-line path, the loop either regenerates fresh cards unnecessarily
(cost) or skips stale ones (user sees yesterday's feed).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from api.feed_loop import _is_fresh_from_map


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_is_fresh_from_map_returns_false_when_line_missing() -> None:
    assert _is_fresh_from_map({}, "physics", 12.0) is False
    assert _is_fresh_from_map({"cuisine": _iso(datetime.now(UTC))}, "physics", 12.0) is False


def test_is_fresh_from_map_returns_true_for_recent_timestamp() -> None:
    recent = datetime.now(UTC) - timedelta(hours=1)
    assert _is_fresh_from_map({"physics": _iso(recent)}, "physics", 12.0) is True


def test_is_fresh_from_map_returns_false_for_stale_timestamp() -> None:
    stale = datetime.now(UTC) - timedelta(hours=48)
    assert _is_fresh_from_map({"physics": _iso(stale)}, "physics", 12.0) is False


def test_is_fresh_from_map_handles_z_suffix_timestamps() -> None:
    """Lake timestamps come back as ...Z style occasionally — parser must
    cope without raising."""
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    assert _is_fresh_from_map({"physics": recent}, "physics", 12.0) is True


def test_is_fresh_from_map_returns_false_on_bad_timestamp() -> None:
    """Unparseable timestamp → treat as stale (safe-default: regenerate)."""
    assert _is_fresh_from_map({"physics": "not-a-date"}, "physics", 12.0) is False


def test_is_fresh_from_map_boundary_exact_freshness_hour() -> None:
    """Exactly at the freshness window: strictly-less-than → stale."""
    boundary = datetime.now(UTC) - timedelta(hours=12)
    assert _is_fresh_from_map({"physics": _iso(boundary)}, "physics", 12.0) is False
