"""Witness ↔ routines plumbing.

Covers:
  · _render_routines_block filters by enabled + host availability
  · empty render when no routines or none eligible
  · prompt template accepts the new {routines_block} slot
"""

from __future__ import annotations

import pytest

from api.loop import witness
from api.loop.prompts import WITNESS_PROMPT


def _routine(rid: str, *, name: str = "", host: str = "", enabled: bool = True,
             schedule: str = "*/5 * * * *", prompt: str = "do thing") -> dict:
    return {
        "id": rid,
        "name": name or rid,
        "enabled": enabled,
        "schedule": schedule,
        "host": host,
        "prompt": prompt,
        "interval_minutes": 0,
        "permission_mode": "auto",
        "single_fire": False,
        "workspace": "",
        "delta_id": f"d-{rid}",
        "last_fire_at": 0,
        "next_fire_at": None,
    }


@pytest.mark.asyncio
async def test_render_empty_when_no_routines(monkeypatch):
    async def _list():
        return []
    from api import routines as rmod
    monkeypatch.setattr(rmod, "list_routines", _list)
    out = await witness._render_routines_block(["fedora"])
    assert out == ""


@pytest.mark.asyncio
async def test_render_excludes_disabled(monkeypatch):
    async def _list():
        return [_routine("on-routine", host="fedora"),
                _routine("off-routine", host="fedora", enabled=False)]
    from api import routines as rmod
    monkeypatch.setattr(rmod, "list_routines", _list)
    out = await witness._render_routines_block(["fedora"])
    assert "on-routine" in out
    assert "off-routine" not in out


@pytest.mark.asyncio
async def test_render_excludes_offline_host(monkeypatch):
    """A routine pinned to a dark host should not appear — firing it
    would just write a fire delta no kitty plugin will pick up."""
    async def _list():
        return [_routine("ramen-check", host="fedora"),
                _routine("dark-routine", host="nixos-server")]
    from api import routines as rmod
    monkeypatch.setattr(rmod, "list_routines", _list)
    out = await witness._render_routines_block(["fedora"])
    assert "ramen-check" in out
    assert "dark-routine" not in out


@pytest.mark.asyncio
async def test_render_keeps_fleet_routines(monkeypatch):
    """Fleet-wide routines (host='') always show — any online agent
    can pick them up."""
    async def _list():
        return [_routine("fleet-routine", host="")]
    from api import routines as rmod
    monkeypatch.setattr(rmod, "list_routines", _list)
    out = await witness._render_routines_block(["fedora"])
    assert "fleet-routine" in out
    assert "(fleet)" in out


@pytest.mark.asyncio
async def test_render_block_contains_route_hint(monkeypatch):
    """Block should mention `routine-fire:<id>` so the model knows the
    syntax it's choosing from."""
    async def _list():
        return [_routine("fleet-routine")]
    from api import routines as rmod
    monkeypatch.setattr(rmod, "list_routines", _list)
    out = await witness._render_routines_block(["fedora"])
    assert "routine-fire:<id>" in out


def test_witness_prompt_accepts_routines_block_slot():
    """Sanity: the template formats with the new slot present."""
    out = WITNESS_PROMPT.format(
        standpoint_block="sp",
        intent_block="i",
        voice_blocks="v",
        anchors_block="a",
        feed_block="f",
        hosts_block="h",
        routines_block="ROUTINES — list here",
    )
    assert "ROUTINES — list here" in out
