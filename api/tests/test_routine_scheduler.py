"""Unit tests for the routine scheduler.

Locks in: cron-elapsed → fires; cron-future → skips; disabled/no-schedule
skipped; single_fire tombstones the spec; hydrate seeds last-fire state
from the lake so a restart doesn't double-fire.

The cron path now fires INTO the river (writes a `routine-due` intent
into the puddle + a `routine-tick` marker into the lake) instead of
calling the legacy `routines.fire()`. Tests mock `write_intent` and
`delta_client.write` directly.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from api import delta_client, routine_scheduler
from api import routines as routines_mod
from api.loop import intents as intents_mod


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
def _capture_writes(monkeypatch):
    """Stub the river-write surfaces and capture what the scheduler emits."""
    intents: list[dict] = []
    ticks: list[dict] = []

    async def _write_intent(*, kind, content, payload=None, extra_tags=None,
                             ttl_seconds=None, source="intent-detector"):
        intents.append({
            "kind": kind,
            "content": content,
            "payload": payload,
            "tags": extra_tags or [],
            "source": source,
        })
        return {"id": f"intent-{len(intents)}"}

    async def _delta_write(content, tags=None, source="fathom-engagement",
                           expires_at=None, media_hash=None):
        ticks.append({
            "content": content,
            "tags": tags or [],
            "source": source,
        })
        return {"id": f"tick-{len(ticks)}"}

    monkeypatch.setattr(routine_scheduler, "write_intent", _write_intent)
    monkeypatch.setattr(routine_scheduler.delta_client, "write", _delta_write)
    return {"intents": intents, "ticks": ticks}


@pytest.mark.asyncio
async def test_fires_into_river_on_elapsed_cron(monkeypatch, _capture_writes):
    """The cron path writes a routine-due intent + a routine-tick marker."""
    async def _specs():
        return [_spec("ramen-check", host="fedora", prompt="check ramen hours")]

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)

    await routine_scheduler._check_once()

    intents = _capture_writes["intents"]
    ticks = _capture_writes["ticks"]
    assert len(intents) == 1
    assert intents[0]["kind"] == "routine-due"
    assert "check ramen hours" in intents[0]["content"]
    assert "routine-id:ramen-check" in intents[0]["tags"]
    assert "host:fedora" in intents[0]["tags"]
    assert intents[0]["payload"]["routine_id"] == "ramen-check"

    assert len(ticks) == 1
    assert "routine-tick" in ticks[0]["tags"]
    assert "routine-id:ramen-check" in ticks[0]["tags"]
    assert "ramen-check" in routine_scheduler._last_fire_at


@pytest.mark.asyncio
async def test_fleet_routine_omits_host_tag(monkeypatch, _capture_writes):
    """A routine with no host pin shouldn't stamp host:<x> on the intent."""
    async def _specs():
        return [_spec("fleet", host="")]

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    await routine_scheduler._check_once()

    intent = _capture_writes["intents"][0]
    assert not any(t.startswith("host:") for t in intent["tags"])


@pytest.mark.asyncio
async def test_does_not_fire_when_just_fired(monkeypatch, _capture_writes):
    routine_scheduler._last_fire_at["ramen-check"] = time.time()

    async def _specs():
        return [_spec("ramen-check")]

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    await routine_scheduler._check_once()
    assert _capture_writes["intents"] == []


@pytest.mark.asyncio
async def test_skips_disabled(monkeypatch, _capture_writes):
    async def _specs():
        return [_spec("ramen-check", enabled=False)]
    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    await routine_scheduler._check_once()
    assert _capture_writes["intents"] == []


@pytest.mark.asyncio
async def test_skips_when_no_schedule(monkeypatch, _capture_writes):
    async def _specs():
        return [_spec("manual-only", schedule="")]
    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    await routine_scheduler._check_once()
    assert _capture_writes["intents"] == []


@pytest.mark.asyncio
async def test_single_fire_soft_deletes(monkeypatch, _capture_writes):
    deleted: list[str] = []

    async def _specs():
        return [_spec("one-shot", single_fire=True)]

    async def _delete_call(rid):
        deleted.append(rid)
        return {"deleted": True}

    monkeypatch.setattr(routines_mod, "_spec_deltas", _specs)
    monkeypatch.setattr(routines_mod, "soft_delete", _delete_call)

    await routine_scheduler._check_once()
    assert len(_capture_writes["intents"]) == 1
    assert deleted == ["one-shot"]


@pytest.mark.asyncio
async def test_skips_tombstoned(monkeypatch, _capture_writes):
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
    assert _capture_writes["intents"] == []


@pytest.mark.asyncio
async def test_hydrate_seeds_last_fires_from_ticks(monkeypatch, _capture_writes):
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
    assert _capture_writes["intents"] == []
