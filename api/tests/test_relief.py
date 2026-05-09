"""Tests for the tiered relief module + partial mark_synthesis."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api import feed_pressure
from api.loop import relief


@pytest.fixture
def relief_state_dir(tmp_path, monkeypatch):
    """Point relief's cooldown file + feed_pressure's anchor at a tmp
    dir so each test starts clean and doesn't touch the real LAKE_DIR."""
    state_path = tmp_path / "feed-pressure-state.json"
    tmp_path / "relief-cooldowns.json"
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


@pytest.mark.asyncio
async def test_pick_tier_bypass_floor_picks_alert_at_zero_pressure(relief_state_dir):
    """Manual button bypass — even at 0 pressure, alert is eligible
    when no cooldowns are active."""
    tier = await relief.pick_tier(pressure_ratio=0.0, bypass_floor=True)
    assert tier is not None
    assert tier["name"] == "alert"


@pytest.mark.asyncio
async def test_pick_tier_bypass_floor_still_respects_cooldown(relief_state_dir, monkeypatch):
    """Bypass floor doesn't bypass cooldown — alert on cooldown still
    skips to bridging even with bypass."""
    from datetime import UTC, datetime

    monkeypatch.setattr(
        relief, "_cooldown_path", lambda: relief_state_dir / "relief-cooldowns.json"
    )
    state = {"alert": datetime.now(UTC).isoformat()}
    relief._save_cooldowns(state)
    tier = await relief.pick_tier(pressure_ratio=0.0, bypass_floor=True)
    assert tier is not None
    assert tier["name"] == "bridging"


# ── sit round-robin ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_sit_picks_reflection(relief_state_dir, monkeypatch):
    """No prior sit fired — round-robin starts at reflection."""
    from datetime import UTC, datetime

    monkeypatch.setattr(
        relief, "_cooldown_path", lambda: relief_state_dir / "relief-cooldowns.json"
    )
    # Fresh state. Stamp alert + bridging as on-cooldown so the picker
    # has to choose between sit flavors.
    now = datetime.now(UTC)
    relief._save_cooldowns(
        {
            "alert": now.isoformat(),
            "bridging": now.isoformat(),
        }
    )
    tier = await relief.pick_tier(pressure_ratio=0.8)
    assert tier is not None
    assert tier["name"] == "reflection"


@pytest.mark.asyncio
async def test_after_reflection_round_robin_picks_drift(relief_state_dir, monkeypatch):
    """After reflection fires, next sit cycle picks drift."""
    from datetime import UTC, datetime

    monkeypatch.setattr(
        relief, "_cooldown_path", lambda: relief_state_dir / "relief-cooldowns.json"
    )
    # Stamp alert + bridging as just-fired so the picker has to choose
    # among sit tiers. Reflection fired in the past (per _last_sit).
    now_iso = datetime.now(UTC).isoformat()
    relief._save_cooldowns(
        {
            "alert": now_iso,
            "bridging": now_iso,
            "_last_sit": "reflection",
        }
    )
    tier = await relief.pick_tier(pressure_ratio=0.8)
    assert tier is not None
    assert tier["name"] == "drift"


@pytest.mark.asyncio
async def test_after_drift_round_robin_picks_reflection(relief_state_dir, monkeypatch):
    """After drift fires, next sit cycle picks reflection."""
    from datetime import UTC, datetime

    monkeypatch.setattr(
        relief, "_cooldown_path", lambda: relief_state_dir / "relief-cooldowns.json"
    )
    now_iso = datetime.now(UTC).isoformat()
    relief._save_cooldowns(
        {
            "alert": now_iso,
            "bridging": now_iso,
            "_last_sit": "drift",
        }
    )
    tier = await relief.pick_tier(pressure_ratio=0.8)
    assert tier is not None
    assert tier["name"] == "reflection"


@pytest.mark.asyncio
async def test_round_robin_falls_through_when_chosen_flavor_on_cooldown(
    relief_state_dir, monkeypatch
):
    """If round-robin says 'drift' but drift is on cooldown and
    reflection isn't, the picker falls through to reflection rather
    than emitting None."""
    from datetime import UTC, datetime, timedelta

    monkeypatch.setattr(
        relief, "_cooldown_path", lambda: relief_state_dir / "relief-cooldowns.json"
    )
    now_iso = datetime.now(UTC).isoformat()
    long_ago = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
    # Drift fired 30 min ago (still on 3h cooldown). _last_sit says
    # reflection so round-robin would point at drift, but drift is
    # locked. Reflection's last fire was 4h ago, so it's available.
    recent_drift = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    relief._save_cooldowns(
        {
            "alert": long_ago,  # past 10-min cooldown — actually still eligible
            "bridging": long_ago,
            "drift": recent_drift,
            "reflection": long_ago,
            "_last_sit": "reflection",  # round-robin would point at drift
        }
    )
    # Force just-fired alert + bridging so only sit tiers are eligible.
    relief._save_cooldowns(
        {
            "alert": now_iso,
            "bridging": now_iso,
            "drift": recent_drift,
            "_last_sit": "reflection",
        }
    )
    tier = await relief.pick_tier(pressure_ratio=0.8)
    # Round-robin says drift, but drift is on cooldown — fallback path
    # picks reflection.
    assert tier is not None
    assert tier["name"] == "reflection"


@pytest.mark.asyncio
async def test_stamp_cooldown_records_last_sit(relief_state_dir, monkeypatch):
    """_stamp_cooldown stamps `_last_sit` only when the tier is in
    SIT_GROUP. Non-sit tiers don't perturb the round-robin pointer."""
    monkeypatch.setattr(
        relief, "_cooldown_path", lambda: relief_state_dir / "relief-cooldowns.json"
    )
    await relief._stamp_cooldown("alert")
    assert relief._load_cooldowns().get("_last_sit") is None
    await relief._stamp_cooldown("reflection")
    assert relief._load_cooldowns().get("_last_sit") == "reflection"
    await relief._stamp_cooldown("drift")
    assert relief._load_cooldowns().get("_last_sit") == "drift"


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
        "volume": 5.0,
        "fresh_volume": 5.0,
        "pressure_floor": 0.0,
        "last_synthesis_at": None,
        "last_wake_at": None,
        "time_since_synthesis_seconds": None,
        "time_since_wake_seconds": None,
        "threshold": 1.0,
        "contrast_wake_seconds": 3600,
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


# ── _fire_single dual-write (Phase 5c) ────────────────────────────


@pytest.mark.asyncio
async def test_fire_single_writes_both_puddle_and_thread():
    """Each pressure tier fire writes a puddle intent (legacy) AND a
    thread user-msg (threaded). Whichever supervisor is active picks
    up the activation; the dormant one ignores its side."""
    tier = next(t for t in relief.RELIEF_TIERS if t["engine"] == "single-fire")

    intent_calls: list[dict] = []
    thread_calls: list[dict] = []

    async def fake_write_intent(**kwargs):
        intent_calls.append(kwargs)
        return {"id": "intent-x"}

    async def fake_thread_append(**kwargs):
        thread_calls.append(kwargs)
        return {"id": "thread-x"}

    with (
        patch("api.loop.relief.write_intent", side_effect=fake_write_intent),
        patch("api.thread.append", side_effect=fake_thread_append),
    ):
        await relief._fire_single(tier, reason="pressure")

    assert len(intent_calls) == 1
    assert intent_calls[0]["kind"] == tier["intent_kind"]
    assert intent_calls[0]["content"] == tier["directive"]

    assert len(thread_calls) == 1
    assert thread_calls[0]["role"] == "user"
    assert thread_calls[0]["msg_kind"] == f"pressure-{tier['name']}"
    assert thread_calls[0]["channel"] == "pressure"
    assert thread_calls[0]["content"] == tier["directive"]
    assert "pressure-tier:" + tier["name"] in (thread_calls[0]["extra_tags"] or [])


@pytest.mark.asyncio
async def test_fire_single_thread_failure_does_not_block_intent_write():
    """Thread write failures are soft — the puddle path is still
    authoritative until full cutover, so a thread hiccup must not
    interrupt the legacy activation."""
    tier = next(t for t in relief.RELIEF_TIERS if t["engine"] == "single-fire")
    intent_calls: list[dict] = []

    async def fake_write_intent(**kwargs):
        intent_calls.append(kwargs)
        return {"id": "intent-x"}

    async def boom(**kwargs):
        raise RuntimeError("lake down")

    with (
        patch("api.loop.relief.write_intent", side_effect=fake_write_intent),
        patch("api.thread.append", side_effect=boom),
    ):
        # Should not raise.
        await relief._fire_single(tier, reason="pressure")

    assert len(intent_calls) == 1


@pytest.mark.asyncio
async def test_fire_single_intent_failure_does_not_block_thread_write():
    """Symmetric — intent write failure shouldn't skip the thread shadow."""
    tier = next(t for t in relief.RELIEF_TIERS if t["engine"] == "single-fire")
    thread_calls: list[dict] = []

    async def fake_thread_append(**kwargs):
        thread_calls.append(kwargs)
        return {"id": "thread-x"}

    async def boom(**kwargs):
        raise RuntimeError("puddle down")

    with (
        patch("api.loop.relief.write_intent", side_effect=boom),
        patch("api.thread.append", side_effect=fake_thread_append),
    ):
        await relief._fire_single(tier, reason="pressure")

    assert len(thread_calls) == 1


# ── _fire_dialogue dual-write ─────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_dialogue_writes_both_puddle_and_thread():
    """Dialogue tiers (reflection, drift) dual-write the seed through
    the same intent + thread path as single-fire tiers, so the threaded
    supervisor can pick them up. The legacy `run_dialogue` path went
    dormant with the Grand Loop migration."""
    tier = next(t for t in relief.RELIEF_TIERS if t["engine"] == "dialogue")

    intent_calls: list[dict] = []
    thread_calls: list[dict] = []

    async def fake_write_intent(**kwargs):
        intent_calls.append(kwargs)
        return {"id": "intent-x"}

    async def fake_thread_append(**kwargs):
        thread_calls.append(kwargs)
        return {"id": "thread-x"}

    with (
        patch("api.loop.relief.write_intent", side_effect=fake_write_intent),
        patch("api.thread.append", side_effect=fake_thread_append),
    ):
        await relief._fire_dialogue(tier, reason="pressure")

    assert len(intent_calls) == 1
    assert intent_calls[0]["kind"] == tier["name"]
    assert intent_calls[0]["content"] == tier["seed"]

    assert len(thread_calls) == 1
    assert thread_calls[0]["role"] == "user"
    assert thread_calls[0]["msg_kind"] == f"pressure-{tier['name']}"
    assert thread_calls[0]["channel"] == "pressure"
    assert thread_calls[0]["content"] == tier["seed"]
    assert "pressure-tier:" + tier["name"] in (thread_calls[0]["extra_tags"] or [])


@pytest.mark.asyncio
async def test_fire_dialogue_thread_failure_does_not_block_intent_write():
    tier = next(t for t in relief.RELIEF_TIERS if t["engine"] == "dialogue")
    intent_calls: list[dict] = []

    async def fake_write_intent(**kwargs):
        intent_calls.append(kwargs)
        return {"id": "intent-x"}

    async def boom(**kwargs):
        raise RuntimeError("lake down")

    with (
        patch("api.loop.relief.write_intent", side_effect=fake_write_intent),
        patch("api.thread.append", side_effect=boom),
    ):
        await relief._fire_dialogue(tier, reason="pressure")

    assert len(intent_calls) == 1


@pytest.mark.asyncio
async def test_fire_dialogue_intent_failure_does_not_block_thread_write():
    tier = next(t for t in relief.RELIEF_TIERS if t["engine"] == "dialogue")
    thread_calls: list[dict] = []

    async def fake_thread_append(**kwargs):
        thread_calls.append(kwargs)
        return {"id": "thread-x"}

    async def boom(**kwargs):
        raise RuntimeError("puddle down")

    with (
        patch("api.loop.relief.write_intent", side_effect=boom),
        patch("api.thread.append", side_effect=fake_thread_append),
    ):
        await relief._fire_dialogue(tier, reason="pressure")

    assert len(thread_calls) == 1


# ── force_tier (per-tier manual fire) ─────────────────────────────


@pytest.mark.asyncio
async def test_fire_relief_force_tier_skips_picker_and_dispatches_directly(
    relief_state_dir, monkeypatch
):
    """`force_tier="reflection"` should bypass the cheapest-first picker
    and fire reflection directly even when alert/bridging would normally
    win on a cheapest-first walk."""
    monkeypatch.setattr(
        relief, "_cooldown_path", lambda: relief_state_dir / "relief-cooldowns.json"
    )

    fired_calls: list[dict] = []

    async def fake_fire_dialogue(tier, reason):
        fired_calls.append({"name": tier["name"], "engine": tier["engine"], "reason": reason})

    async def fake_fire_single(tier, reason):
        fired_calls.append({"name": tier["name"], "engine": tier["engine"], "reason": reason})

    async def fake_pressure():
        return {"volume": 0.0, "threshold": 100.0}

    async def fake_mark_synthesis(weight=1.0):
        return None

    with (
        patch.object(relief, "_fire_dialogue", side_effect=fake_fire_dialogue),
        patch.object(relief, "_fire_single", side_effect=fake_fire_single),
        patch.object(relief.feed_pressure, "read_pressure", side_effect=fake_pressure),
        patch.object(relief.feed_pressure, "mark_synthesis", side_effect=fake_mark_synthesis),
    ):
        result = await relief.fire_relief("manual", bypass_floor=True, force_tier="reflection")

    assert result["fired"] == "reflection"
    assert len(fired_calls) == 1
    assert fired_calls[0]["name"] == "reflection"
    assert fired_calls[0]["engine"] == "dialogue"


@pytest.mark.asyncio
async def test_fire_relief_force_tier_returns_on_cooldown_when_locked(
    relief_state_dir, monkeypatch
):
    """A locked tier returns `on_cooldown` rather than picking another
    tier. Manual buttons should reflect their own state, not redirect."""
    from datetime import UTC, datetime

    monkeypatch.setattr(
        relief, "_cooldown_path", lambda: relief_state_dir / "relief-cooldowns.json"
    )
    state = {"reflection": datetime.now(UTC).isoformat()}
    relief._save_cooldowns(state)

    fired_calls: list[dict] = []

    async def fake_fire(_tier, _reason):
        fired_calls.append({})

    async def fake_pressure():
        return {"volume": 0.0, "threshold": 100.0}

    with (
        patch.object(relief, "_fire_dialogue", side_effect=fake_fire),
        patch.object(relief, "_fire_single", side_effect=fake_fire),
        patch.object(relief.feed_pressure, "read_pressure", side_effect=fake_pressure),
    ):
        result = await relief.fire_relief("manual", bypass_floor=True, force_tier="reflection")

    assert result["fired"] is None
    assert result["reason"] == "on_cooldown"
    assert result["tier"] == "reflection"
    assert fired_calls == []  # No fire ran.


@pytest.mark.asyncio
async def test_fire_relief_force_tier_unknown_returns_unknown_tier(relief_state_dir):
    """Unknown tier name returns `unknown_tier` — defensive against
    typos or stale UI state."""
    fired_calls: list[dict] = []

    async def fake_fire(_tier, _reason):
        fired_calls.append({})

    async def fake_pressure():
        return {"volume": 0.0, "threshold": 100.0}

    with (
        patch.object(relief, "_fire_dialogue", side_effect=fake_fire),
        patch.object(relief, "_fire_single", side_effect=fake_fire),
        patch.object(relief.feed_pressure, "read_pressure", side_effect=fake_pressure),
    ):
        result = await relief.fire_relief("manual", force_tier="meditation")

    assert result["fired"] is None
    assert result["reason"] == "unknown_tier"
    assert result["requested_tier"] == "meditation"
    assert fired_calls == []


@pytest.mark.asyncio
async def test_fire_relief_force_tier_bypasses_alert_recency_suppression(
    relief_state_dir, monkeypatch
):
    """Force-firing alert manually should still go through pick_tier's
    recency check — that's a structural protection, not a floor concern."""
    # Existing recency check only fires inside fire_relief when
    # tier=alert. We confirm force_tier="alert" still triggers it.
    monkeypatch.setattr(
        relief, "_cooldown_path", lambda: relief_state_dir / "relief-cooldowns.json"
    )

    fired_calls: list[dict] = []

    async def fake_fire_single(tier, reason):
        fired_calls.append({"name": tier["name"]})

    async def fake_pressure():
        return {"volume": 0.0, "threshold": 100.0}

    async def fake_mark_synthesis(weight=1.0):
        return None

    async def fake_recent_alert_id():
        return ""  # No recent alert → not suppressed → fires.

    with (
        patch.object(relief, "_fire_single", side_effect=fake_fire_single),
        patch.object(relief, "_recent_alert_id", side_effect=fake_recent_alert_id),
        patch.object(relief.feed_pressure, "read_pressure", side_effect=fake_pressure),
        patch.object(relief.feed_pressure, "mark_synthesis", side_effect=fake_mark_synthesis),
    ):
        result = await relief.fire_relief("manual", bypass_floor=True, force_tier="alert")

    assert result["fired"] == "alert"
    assert fired_calls[0]["name"] == "alert"
