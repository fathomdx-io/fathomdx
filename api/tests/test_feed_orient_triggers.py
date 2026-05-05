"""Tests for feed-orient regen trigger logic.

Two trigger paths:
  · primary  — N engagements since last regen (default 10)
  · stale    — ≥STALE_REGEN_AGE_S since last regen AND ≥2 engagements

Stale exists for the post-migration regime where engagement-rate
collapsed (159/day → 1-3/day) and the primary 10-engagement floor
stops tripping. Mood's shift-overflow secondary trigger is the
pattern this mirrors.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from api.loop import feed_orient


def _prior(timestamp_iso: str | None = None) -> dict | None:
    if timestamp_iso is None:
        return None
    return {"id": "carrier-x", "timestamp": timestamp_iso}


# ── primary trigger ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_below_primary_and_not_stale_returns_below_threshold() -> None:
    """5 engagements + 1 hour since last regen → both triggers cold."""
    one_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    with patch.object(feed_orient, "_latest_feed_orient", return_value=_prior(one_hour_ago)), \
         patch.object(feed_orient, "_engagements_since", return_value=[{"id": f"e{i}"} for i in range(5)]), \
         patch.object(feed_orient, "_run_regen", return_value=True), \
         patch.object(feed_orient, "feed_orient_drift") as drift_mock, \
         patch.object(feed_orient, "feed_orient_confidence") as conf_mock:
        async def _no_op():
            return None
        drift_mock.sample = _no_op
        conf_mock.sample = _no_op

        # Bypass MIN_COOLDOWN_S — 1 hour is past the 30min cooldown.
        result = await feed_orient._check_once()

    assert result["feed_orient"] == "below-threshold"
    assert result["count"] == 5


@pytest.mark.asyncio
async def test_check_at_primary_threshold_fires() -> None:
    """10 engagements + recent prior → primary trigger fires."""
    one_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    with patch.object(feed_orient, "_latest_feed_orient", return_value=_prior(one_hour_ago)), \
         patch.object(feed_orient, "_engagements_since", return_value=[{"id": f"e{i}"} for i in range(10)]), \
         patch.object(feed_orient, "_run_regen", return_value=True), \
         patch.object(feed_orient, "feed_orient_drift") as drift_mock, \
         patch.object(feed_orient, "feed_orient_confidence") as conf_mock:
        async def _no_op():
            return None
        drift_mock.sample = _no_op
        conf_mock.sample = _no_op

        result = await feed_orient._check_once()

    assert result["feed_orient"] == "fired"
    assert result["reason"] == "primary"


# ── stale-regen secondary trigger ─────────────────────────────────


@pytest.mark.asyncio
async def test_check_stale_with_minimum_engagement_fires() -> None:
    """5 days since last regen + 3 engagements → stale trigger fires
    even though the primary 10-engagement floor isn't met."""
    five_days_ago = (datetime.now(UTC) - timedelta(days=5)).isoformat()

    with patch.object(feed_orient, "_latest_feed_orient", return_value=_prior(five_days_ago)), \
         patch.object(feed_orient, "_engagements_since", return_value=[{"id": f"e{i}"} for i in range(3)]), \
         patch.object(feed_orient, "_run_regen", return_value=True), \
         patch.object(feed_orient, "feed_orient_drift") as drift_mock, \
         patch.object(feed_orient, "feed_orient_confidence") as conf_mock:
        async def _no_op():
            return None
        drift_mock.sample = _no_op
        conf_mock.sample = _no_op

        result = await feed_orient._check_once()

    assert result["feed_orient"] == "fired"
    assert result["reason"] == "stale"


@pytest.mark.asyncio
async def test_check_stale_with_zero_engagement_does_not_fire() -> None:
    """5 days since last regen + 0 engagements → stale trigger HOLDS.
    The min-engagement guard keeps the regen from firing on truly
    dead substrate (no signal at all → nothing to orient against)."""
    five_days_ago = (datetime.now(UTC) - timedelta(days=5)).isoformat()

    with patch.object(feed_orient, "_latest_feed_orient", return_value=_prior(five_days_ago)), \
         patch.object(feed_orient, "_engagements_since", return_value=[]), \
         patch.object(feed_orient, "_run_regen", return_value=True), \
         patch.object(feed_orient, "feed_orient_drift") as drift_mock, \
         patch.object(feed_orient, "feed_orient_confidence") as conf_mock:
        async def _no_op():
            return None
        drift_mock.sample = _no_op
        conf_mock.sample = _no_op

        result = await feed_orient._check_once()

    assert result["feed_orient"] == "below-threshold"
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_check_two_days_old_with_three_engagements_does_not_fire_stale() -> None:
    """2 days < STALE_REGEN_AGE_S (3 days) — stale trigger holds even
    with engagements present. Primary still misses with only 3."""
    two_days_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat()

    with patch.object(feed_orient, "_latest_feed_orient", return_value=_prior(two_days_ago)), \
         patch.object(feed_orient, "_engagements_since", return_value=[{"id": f"e{i}"} for i in range(3)]), \
         patch.object(feed_orient, "_run_regen", return_value=True), \
         patch.object(feed_orient, "feed_orient_drift") as drift_mock, \
         patch.object(feed_orient, "feed_orient_confidence") as conf_mock:
        async def _no_op():
            return None
        drift_mock.sample = _no_op
        conf_mock.sample = _no_op

        result = await feed_orient._check_once()

    assert result["feed_orient"] == "below-threshold"
    assert result["count"] == 3


# ── cooldown short-circuit ────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_within_cooldown_returns_cooldown_without_firing() -> None:
    """Within the 30-minute cooldown window → return cooldown,
    don't even count engagements."""
    ten_min_ago = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()

    engagements_called: list[bool] = []

    async def _engagements_spy(_):
        engagements_called.append(True)
        return [{"id": f"e{i}"} for i in range(50)]

    with patch.object(feed_orient, "_latest_feed_orient", return_value=_prior(ten_min_ago)), \
         patch.object(feed_orient, "_engagements_since", side_effect=_engagements_spy), \
         patch.object(feed_orient, "_run_regen", return_value=True), \
         patch.object(feed_orient, "feed_orient_drift") as drift_mock, \
         patch.object(feed_orient, "feed_orient_confidence") as conf_mock:
        async def _no_op():
            return None
        drift_mock.sample = _no_op
        conf_mock.sample = _no_op

        result = await feed_orient._check_once()

    assert result["feed_orient"] == "cooldown"
    assert engagements_called == []
