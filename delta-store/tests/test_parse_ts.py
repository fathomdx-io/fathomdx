"""Unit tests for plan._parse_ts.

Used by every plan step that takes time bounds (`_exec_search`,
`_exec_filter`, `_exec_neighbors`) plus the aggregate bucketing path.
A regression here corrupts time-window queries silently — wrong bucket,
wrong neighbor radius, wrong filter bounds — without raising.
"""

from __future__ import annotations

from datetime import UTC, datetime

from deltas.plan import _parse_ts


def test_parses_utc_z_suffix() -> None:
    """The lake serializes timestamps with a trailing 'Z'. fromisoformat
    doesn't accept 'Z' on Python < 3.11; we substitute '+00:00' first."""
    assert _parse_ts("2026-04-28T12:00:00Z") == datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def test_parses_utc_explicit_offset() -> None:
    assert _parse_ts("2026-04-28T12:00:00+00:00") == datetime(
        2026, 4, 28, 12, 0, 0, tzinfo=UTC
    )


def test_naive_input_is_assumed_utc() -> None:
    """A bare ISO string with no tz info is treated as UTC. Lake writes
    are UTC; this is the common shape."""
    assert _parse_ts("2026-04-28T12:00:00") == datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def test_non_utc_offset_is_converted_not_relabelled() -> None:
    """REGRESSION GUARD. The previous implementation did
    `.replace(tzinfo=UTC)` which kept the wallclock unchanged and swapped
    the timezone label — silently shifting `12:00 -08:00` to `12:00 UTC`
    (an 8-hour drift). The fix uses `.astimezone(UTC)` so the wallclock
    moves to its UTC equivalent. If this test fails, every time-window
    query that accepts a non-UTC bound is wrong by the offset.
    """
    expected = datetime(2026, 4, 28, 20, 0, 0, tzinfo=UTC)  # 12:00 -08:00 == 20:00 UTC
    assert _parse_ts("2026-04-28T12:00:00-08:00") == expected


def test_non_utc_offset_round_trip() -> None:
    """Equivalent UTC and offset representations of the same instant
    parse to the same datetime."""
    a = _parse_ts("2026-04-28T20:00:00Z")
    b = _parse_ts("2026-04-28T12:00:00-08:00")
    c = _parse_ts("2026-04-28T22:00:00+02:00")
    assert a == b == c


def test_microseconds_preserved() -> None:
    """ISO timestamps with sub-second precision shouldn't lose digits."""
    parsed = _parse_ts("2026-04-28T12:00:00.123456Z")
    assert parsed == datetime(2026, 4, 28, 12, 0, 0, 123456, tzinfo=UTC)
