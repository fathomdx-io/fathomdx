"""Phase 4b tests — session-aware settle threshold drift.

Reads recent puddle metric deltas, computes a drifted threshold so
hard-problem hours loosen and quiet hours tighten. Falls back to
the static default with too few samples.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from api.loop import metric as metric_mod
from api.loop.metric import (
    SETTLE_SPREAD_MAX,
    _DRIFT_MIN_SAMPLES,
    session_aware_spread_max,
    settle_window_check,
)


# ── settle_window_check with drifted threshold ─────────────────────


def test_settle_check_uses_default_when_no_drift_passed() -> None:
    samples = [0.05, 0.06, 0.07, 0.05, 0.06]
    ok, level = settle_window_check(samples)
    assert ok
    assert level is not None


def test_settle_check_respects_passed_threshold() -> None:
    """Same samples that settle under default 0.12 should NOT settle
    when threshold is tightened to 0.005."""
    samples = [0.05, 0.06, 0.07, 0.05, 0.06]
    ok, _ = settle_window_check(samples, spread_max=0.005)
    assert not ok


def test_settle_check_loose_threshold_settles_what_default_does_not() -> None:
    """Samples that don't settle under default 0.12 should settle
    when threshold is loosened to 0.20."""
    samples = [0.05, 0.10, 0.12, 0.15, 0.18]
    ok_default, _ = settle_window_check(samples)
    assert not ok_default
    ok_loose, _ = settle_window_check(samples, spread_max=0.20)
    assert ok_loose


def test_settle_check_clamps_threshold_above_floor() -> None:
    """Threshold of 0 should clamp to a small floor — otherwise the
    check would never settle."""
    samples = [0.0, 0.0, 0.0, 0.0, 0.0]
    ok, _ = settle_window_check(samples, spread_max=0.0)
    # Even at clamped floor the spread is exactly 0, so this passes.
    assert ok


# ── session_aware_spread_max ───────────────────────────────────────


def _metric_delta(distance: float, ts: str = "2026-04-29T14:00:00Z") -> dict:
    return {
        "id": "m",
        "tags": ["metric"],
        "content": json.dumps({"distance": distance, "voice": "creator"}),
        "timestamp": ts,
    }


def test_drift_returns_default_with_no_samples() -> None:
    with patch.object(metric_mod, "puddle") as puddle_mock:
        puddle_mock.query.return_value = []
        out = session_aware_spread_max()
    assert out == SETTLE_SPREAD_MAX


def test_drift_returns_default_below_min_samples() -> None:
    with patch.object(metric_mod, "puddle") as puddle_mock:
        puddle_mock.query.return_value = [_metric_delta(0.1)] * (_DRIFT_MIN_SAMPLES - 1)
        out = session_aware_spread_max()
    assert out == SETTLE_SPREAD_MAX


def test_drift_loosens_when_recent_spreads_run_high() -> None:
    """Hard-problem session — recent metrics consistently 0.20.
    Threshold should drift UP from default 0.12 toward 0.20."""
    rows = [_metric_delta(0.20)] * 20
    with patch.object(metric_mod, "puddle") as puddle_mock:
        puddle_mock.query.return_value = rows
        out = session_aware_spread_max()
    assert out > SETTLE_SPREAD_MAX


def test_drift_tightens_when_recent_spreads_run_low() -> None:
    """Quiet session — recent metrics consistently 0.04. Threshold
    should drift DOWN from default 0.12 toward 0.04."""
    rows = [_metric_delta(0.04)] * 20
    with patch.object(metric_mod, "puddle") as puddle_mock:
        puddle_mock.query.return_value = rows
        out = session_aware_spread_max()
    assert out < SETTLE_SPREAD_MAX


def test_drift_clamps_at_50_to_150_pct_of_default() -> None:
    """A wild outlier session shouldn't drive the threshold to 0 or
    to a value where nothing ever settles."""
    rows_low = [_metric_delta(0.0)] * 30
    with patch.object(metric_mod, "puddle") as puddle_mock:
        puddle_mock.query.return_value = rows_low
        low_out = session_aware_spread_max()
    assert low_out >= SETTLE_SPREAD_MAX * 0.5

    rows_high = [_metric_delta(2.0)] * 30
    with patch.object(metric_mod, "puddle") as puddle_mock:
        puddle_mock.query.return_value = rows_high
        high_out = session_aware_spread_max()
    assert high_out <= SETTLE_SPREAD_MAX * 1.5


def test_drift_soft_fails_to_default_on_puddle_error() -> None:
    with patch.object(metric_mod, "puddle") as puddle_mock:
        puddle_mock.query.side_effect = RuntimeError("puddle locked")
        out = session_aware_spread_max()
    assert out == SETTLE_SPREAD_MAX


def test_drift_skips_unparseable_content() -> None:
    """Malformed metric deltas (not JSON, missing distance) are
    skipped, not crash. If too few valid samples remain, default."""
    rows = [
        _metric_delta(0.10),
        {"id": "bad", "tags": ["metric"], "content": "not json", "timestamp": "t"},
        {"id": "bad2", "tags": ["metric"], "content": '{}', "timestamp": "t"},
    ] + [_metric_delta(0.10)] * 5
    with patch.object(metric_mod, "puddle") as puddle_mock:
        puddle_mock.query.return_value = rows
        # Only 6 valid samples, below MIN — default.
        out = session_aware_spread_max()
    assert out == SETTLE_SPREAD_MAX
