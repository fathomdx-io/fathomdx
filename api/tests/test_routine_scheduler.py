"""Unit tests for the routine scheduler.

Locks in: cron-elapsed → fires; cron-future → skips; disabled/no-schedule
skipped; single_fire tombstones the spec; hydrate seeds last-fire state
from the lake so a restart doesn't double-fire.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from api import routine_scheduler
from api import routines as routines_mod


def _spec(rid: str, *, schedule: str = "*/5 * * * *", enabled: bool = True,
          single_fire: bool = False, ts: str | None = None) -> dict:
    """Build a spec-delta shape matching what _spec_deltas() returns."""
    meta = {
        "id": rid,
        "name": rid,
        "schedule": schedule,
        "enabled": enabled,
        "single_fire": single_fire,
    }
    return {
        "id": f"delta-{rid}",
        "tags": ["spec", "routine", f"routine-id:{rid}"],
        "content": routines_mod.render_frontmatter(meta, "do the thing"),
        "timestamp": ts or datetime.now(UTC).isoformat(),
    }


def _fire(rid: str, ts: datetime) -> dict:
    return {
        "id": f"fire-{rid}-{ts.timestamp():.0f}",
        "tags": ["routine-fire", f"routine-id:{rid}"],
        "content": "fired",
        "timestamp": ts.isoformat(),
    }


@pytest.fixture(autouse=True)
def _reset_state():
    routine_scheduler._last_fire_at = {}
    routine_scheduler._boot_time = time.time() - 3600  # boot was an hour ago
    yield
    routine_scheduler._last_fire_at = {}
    routine_scheduler._boot_time = 0.0


@pytest.mark.asyncio
async def test_fires_when_cron_elapsed(monkeypatch):
    """Routine with no last-fire and cron */5 fires on first tick (boot
    was 1h ago, so the next */5 boundary is well in the past)."""
    fired: list[str] = []

    async def _specs():
        return [_spec("ramen-check")]

    async def _fire_call(rid, prompt_override=None):
        fired.append(rid)
        return {"fired": True, "routine_id": rid, "fire_delta_id": "x"}

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    monkeypatch.setattr(routines_mod, "fire", _fire_call)

    await routine_scheduler._check_once()
    assert fired == ["ramen-check"]
    assert "ramen-check" in routine_scheduler._last_fire_at


@pytest.mark.asyncio
async def test_does_not_fire_when_just_fired(monkeypatch):
    """If we already fired within the current cron window, don't refire."""
    fired: list[str] = []
    routine_scheduler._last_fire_at["ramen-check"] = time.time()  # just now

    async def _specs():
        return [_spec("ramen-check")]

    async def _fire_call(rid, prompt_override=None):
        fired.append(rid)
        return {"fired": True}

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    monkeypatch.setattr(routines_mod, "fire", _fire_call)

    await routine_scheduler._check_once()
    assert fired == []


@pytest.mark.asyncio
async def test_skips_disabled(monkeypatch):
    fired: list[str] = []

    async def _specs():
        return [_spec("ramen-check", enabled=False)]

    async def _fire_call(rid, prompt_override=None):
        fired.append(rid)
        return {"fired": True}

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    monkeypatch.setattr(routines_mod, "fire", _fire_call)

    await routine_scheduler._check_once()
    assert fired == []


@pytest.mark.asyncio
async def test_skips_when_no_schedule(monkeypatch):
    fired: list[str] = []

    async def _specs():
        return [_spec("manual-only", schedule="")]

    async def _fire_call(rid, prompt_override=None):
        fired.append(rid)
        return {"fired": True}

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    monkeypatch.setattr(routines_mod, "fire", _fire_call)

    await routine_scheduler._check_once()
    assert fired == []


@pytest.mark.asyncio
async def test_single_fire_soft_deletes(monkeypatch):
    fired: list[str] = []
    deleted: list[str] = []

    async def _specs():
        return [_spec("one-shot", single_fire=True)]

    async def _fire_call(rid, prompt_override=None):
        fired.append(rid)
        return {"fired": True}

    async def _delete_call(rid):
        deleted.append(rid)
        return {"deleted": True}

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    monkeypatch.setattr(routines_mod, "fire", _fire_call)
    monkeypatch.setattr(routines_mod, "soft_delete", _delete_call)

    await routine_scheduler._check_once()
    assert fired == ["one-shot"]
    assert deleted == ["one-shot"]


@pytest.mark.asyncio
async def test_skips_tombstoned(monkeypatch):
    """A spec with deleted:true must never fire, even if cron has elapsed."""
    fired: list[str] = []

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

    async def _fire_call(rid, prompt_override=None):
        fired.append(rid)
        return {"fired": True}

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    monkeypatch.setattr(routines_mod, "fire", _fire_call)

    await routine_scheduler._check_once()
    assert fired == []


@pytest.mark.asyncio
async def test_hydrate_seeds_last_fires_from_lake(monkeypatch):
    """A restart-then-tick must not re-fire a routine whose last lake-fire
    is recent enough to still be within its cron window."""
    fired: list[str] = []

    just_fired_at = datetime.now(UTC) - timedelta(seconds=10)

    async def _fires():
        return [_fire("ramen-check", just_fired_at)]

    async def _specs():
        return [_spec("ramen-check")]

    async def _fire_call(rid, prompt_override=None):
        fired.append(rid)
        return {"fired": True}

    monkeypatch.setattr(routines_mod, "_fire_deltas", _fires)
    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    monkeypatch.setattr(routines_mod, "fire", _fire_call)

    await routine_scheduler._hydrate_last_fires()
    assert routine_scheduler._last_fire_at["ramen-check"] >= just_fired_at.timestamp() - 1

    await routine_scheduler._check_once()
    assert fired == []
