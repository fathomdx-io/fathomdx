"""Tests for the tiered relief module + partial mark_synthesis."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from api import feed_pressure
from api.loop import relief


@pytest.fixture
def relief_state_dir(tmp_path, monkeypatch):
    """Point relief's cooldown file + feed_pressure's anchor at a tmp
    dir so each test starts clean and doesn't touch the real LAKE_DIR."""
    state_path = tmp_path / "feed-pressure-state.json"
    cooldown_path = tmp_path / "relief-cooldowns.json"
    monkeypatch.setattr(
        feed_pressure.settings,
        "feed_pressure_state_path",
        str(state_path),
    )
    return tmp_path


# ── pick_tier ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pick_tier_below_lowest_floor(relief_state_dir):
    """Pressure below alert's 0.3 floor → no tier fires."""
    tier = await relief.pick_tier(pressure_ratio=0.1)
    assert tier is None


@pytest.mark.asyncio
async def test_pick_tier_picks_alert_at_low_pressure(relief_state_dir):
    """At ratio 0.4 only alert is eligible (bridging needs 0.5)."""
    tier = await relief.pick_tier(pressure_ratio=0.4)
    assert tier is not None
    assert tier["name"] == "alert"


@pytest.mark.asyncio
async def test_pick_tier_picks_lowest_eligible_at_high_pressure(relief_state_dir):
    """At ratio 1.0 every tier is eligible — picker returns alert
    (lowest in the table). Pressure that survives the cooldown
    elevates next tick."""
    tier = await relief.pick_tier(pressure_ratio=1.0)
    assert tier is not None
    assert tier["name"] == "alert"


@pytest.mark.asyncio
async def test_pick_tier_skips_cooled_down_tier(relief_state_dir, monkeypatch):
    """If alert is on cooldown, picker skips it and picks bridging."""
    from datetime import UTC, datetime

    # Stamp alert as just-fired.
    monkeypatch.setattr(
        relief, "_cooldown_path", lambda: relief_state_dir / "relief-cooldowns.json"
    )
    state = {"alert": datetime.now(UTC).isoformat()}
    relief._save_cooldowns(state)
    tier = await relief.pick_tier(pressure_ratio=0.6)
    assert tier is not None
    assert tier["name"] == "bridging"


@pytest.mark.asyncio
async def test_pick_tier_returns_none_when_all_cooled(relief_state_dir, monkeypatch):
    """All tiers on cooldown → no fire."""
    from datetime import UTC, datetime

    monkeypatch.setattr(
        relief, "_cooldown_path", lambda: relief_state_dir / "relief-cooldowns.json"
    )
    now_iso = datetime.now(UTC).isoformat()
    state = {t["name"]: now_iso for t in relief.RELIEF_TIERS}
    relief._save_cooldowns(state)
    tier = await relief.pick_tier(pressure_ratio=1.0)
    assert tier is None


# ── partial mark_synthesis ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_synthesis_default_zeroes_floor(relief_state_dir):
    """weight=1.0 (default) clears any prior floor."""
    # Pre-set a floor.
    state = feed_pressure._load_raw()
    state["pressure_floor"] = 7.5
    feed_pressure._save_raw(state)

    await feed_pressure.mark_synthesis()  # default weight=1.0

    after = feed_pressure._load_raw()
    assert after["pressure_floor"] == 0.0


@pytest.mark.asyncio
async def test_mark_synthesis_partial_carries_remainder(relief_state_dir):
    """weight=0.3 with pre-fire pressure of 10 leaves 7 as floor."""
    # Stub read_pressure to report a known pre-fire volume.
    fake_pressure = {
        "volume": 10.0,
        "fresh_volume": 10.0,
        "pressure_floor": 0.0,
        "last_synthesis_at": None,
        "last_wake_at": None,
        "time_since_synthesis_seconds": None,
        "time_since_wake_seconds": None,
        "threshold": 1.0,
        "contrast_wake_seconds": 3600,
    }
    with patch.object(feed_pressure, "read_pressure", AsyncMock(return_value=fake_pressure)):
        await feed_pressure.mark_synthesis(weight=0.3)

    after = feed_pressure._load_raw()
    # 10 * (1 - 0.3) = 7
    assert after["pressure_floor"] == pytest.approx(7.0, rel=0.01)


@pytest.mark.asyncio
async def test_mark_synthesis_clamps_negative_weight(relief_state_dir):
    """weight<0 is clamped to 0 (no consumption — floor = full pressure)."""
    fake_pressure = {
        "volume": 5.0, "fresh_volume": 5.0, "pressure_floor": 0.0,
        "last_synthesis_at": None, "last_wake_at": None,
        "time_since_synthesis_seconds": None, "time_since_wake_seconds": None,
        "threshold": 1.0, "contrast_wake_seconds": 3600,
    }
    with patch.object(feed_pressure, "read_pressure", AsyncMock(return_value=fake_pressure)):
        await feed_pressure.mark_synthesis(weight=-0.5)
    after = feed_pressure._load_raw()
    # weight clamped to 0 → carry 100%
    assert after["pressure_floor"] == pytest.approx(5.0, rel=0.01)


@pytest.mark.asyncio
async def test_mark_synthesis_clamps_oversized_weight(relief_state_dir):
    """weight>1 is clamped to 1 (full consumption)."""
    state = feed_pressure._load_raw()
    state["pressure_floor"] = 5.0
    feed_pressure._save_raw(state)

    await feed_pressure.mark_synthesis(weight=2.5)

    after = feed_pressure._load_raw()
    assert after["pressure_floor"] == 0.0


# ── tier ordering invariants ───────────────────────────────────────────


def test_tiers_listed_cheapest_first():
    """The picker depends on bottom-up ordering. Don't reorder without
    intent."""
    weights = [t["consume_weight"] for t in relief.RELIEF_TIERS]
    assert weights == sorted(weights), (
        f"RELIEF_TIERS must be ordered by consume_weight ascending, got {weights}"
    )


def test_tiers_have_required_fields():
    """Each tier must declare engine, floor, weight, cooldown."""
    required = {"name", "engine", "min_pressure_ratio", "consume_weight", "cooldown_seconds"}
    for tier in relief.RELIEF_TIERS:
        missing = required - tier.keys()
        assert not missing, f"tier {tier.get('name')} missing fields: {missing}"


def test_single_fire_tiers_have_directive_and_intent_kind():
    for tier in relief.RELIEF_TIERS:
        if tier["engine"] == "single-fire":
            assert tier.get("directive"), f"{tier['name']}: single-fire needs directive"
            assert tier.get("intent_kind"), f"{tier['name']}: single-fire needs intent_kind"


def test_dialogue_tiers_have_seed():
    for tier in relief.RELIEF_TIERS:
        if tier["engine"] == "dialogue":
            assert tier.get("seed"), f"{tier['name']}: dialogue tier needs seed"
