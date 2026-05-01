"""Unit tests for the routine scheduler.

Locks in: cron-elapsed → fires; cron-future → skips; disabled/no-schedule
skipped; single_fire tombstones the spec; hydrate seeds last-fire state
from the lake so a restart doesn't double-fire.

The cron path delegates to `routines.fire()`, which writes a `routine-due`
intent into the puddle + a `routine-tick` marker into the lake. Tests
mock `routines.fire()` directly to capture what fired.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from api import delta_client, routine_scheduler
from api import routines as routines_mod


def _spec(rid: str, *, schedule: str = "*/5 * * * *", enabled: bool = True,
          single_fire: bool = False, ts: str | None = None,
          host: str = "", prompt: str = "do the thing") -> dict:
    """Build a spec-delta shape matching what _spec_deltas() returns."""
    meta = {
        "id": rid,
        "name": rid,
        "schedule": schedule,
        "enabled": enabled,
        "single_fire": single_fire,
    }
    if host:
        meta["host"] = host
    return {
        "id": f"delta-{rid}",
        "tags": ["spec", "routine", f"routine-id:{rid}"],
        "content": routines_mod.render_frontmatter(meta, prompt),
        "timestamp": ts or datetime.now(UTC).isoformat(),
    }


def _tick(rid: str, ts: datetime) -> dict:
    return {
        "id": f"tick-{rid}-{ts.timestamp():.0f}",
        "tags": ["routine-tick", f"routine-id:{rid}"],
        "content": f"routine-tick: {rid}",
        "timestamp": ts.isoformat(),
    }


@pytest.fixture(autouse=True)
def _reset_state():
    routine_scheduler._last_fire_at = {}
    routine_scheduler._boot_time = time.time() - 3600  # boot was an hour ago
    yield
    routine_scheduler._last_fire_at = {}
    routine_scheduler._boot_time = 0.0


@pytest.fixture
def _capture_fires(monkeypatch):
    """Capture calls to routines.fire — what the scheduler delegates to."""
    fires: list[str] = []

    async def _fire(rid, prompt_override=None):
        fires.append(rid)
        return {"fired": True, "routine_id": rid, "intent_id": f"i-{rid}"}

    monkeypatch.setattr(routines_mod, "fire", _fire)
    return fires


@pytest.mark.asyncio
async def test_fires_into_river_on_elapsed_cron(monkeypatch, _capture_fires):
    async def _specs():
        return [_spec("ramen-check", host="fedora", prompt="check ramen hours")]

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)

    await routine_scheduler._check_once()

    assert _capture_fires == ["ramen-check"]
    assert "ramen-check" in routine_scheduler._last_fire_at


@pytest.mark.asyncio
async def test_does_not_fire_when_just_fired(monkeypatch, _capture_fires):
    routine_scheduler._last_fire_at["ramen-check"] = time.time()

    async def _specs():
        return [_spec("ramen-check")]

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    await routine_scheduler._check_once()
    assert _capture_fires == []


@pytest.mark.asyncio
async def test_skips_disabled(monkeypatch, _capture_fires):
    async def _specs():
        return [_spec("ramen-check", enabled=False)]
    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    await routine_scheduler._check_once()
    assert _capture_fires == []


@pytest.mark.asyncio
async def test_skips_when_no_schedule(monkeypatch, _capture_fires):
    async def _specs():
        return [_spec("manual-only", schedule="")]
    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    await routine_scheduler._check_once()
    assert _capture_fires == []


@pytest.mark.asyncio
async def test_single_fire_soft_deletes(monkeypatch, _capture_fires):
    deleted: list[str] = []

    async def _specs():
        return [_spec("one-shot", single_fire=True)]

    async def _delete_call(rid):
        deleted.append(rid)
        return {"deleted": True}

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    monkeypatch.setattr(routines_mod, "soft_delete", _delete_call)

    await routine_scheduler._check_once()
    assert _capture_fires == ["one-shot"]
    assert deleted == ["one-shot"]


@pytest.mark.asyncio
async def test_skips_tombstoned(monkeypatch, _capture_fires):
    meta = {
        "id": "old-routine",
        "name": "old",
        "schedule": "*/5 * * * *",
        "enabled": True,
        "deleted": True,
    }
    spec = {
        "id": "delta-old",
        "tags": ["spec", "routine", "routine-id:old-routine"],
        "content": routines_mod.render_frontmatter(meta, ""),
        "timestamp": datetime.now(UTC).isoformat(),
    }

    async def _specs():
        return [spec]
    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    await routine_scheduler._check_once()
    assert _capture_fires == []


@pytest.mark.asyncio
async def test_hydrate_seeds_last_fires_from_ticks(monkeypatch, _capture_fires):
    """A restart-then-tick must not re-fire a routine whose last
    routine-tick is recent enough to still be within its cron window."""
    just_fired_at = datetime.now(UTC) - timedelta(seconds=10)

    async def _query(limit=500, tags_include=None, **_kwargs):
        if tags_include and "routine-tick" in tags_include:
            return [_tick("ramen-check", just_fired_at)]
        return []

    async def _specs():
        return [_spec("ramen-check")]

    monkeypatch.setattr(delta_client, "query", _query)
    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)

    await routine_scheduler._hydrate_last_fires()
    assert (routine_scheduler._last_fire_at["ramen-check"]
            >= just_fired_at.timestamp() - 1)

    await routine_scheduler._check_once()
    assert _capture_fires == []
