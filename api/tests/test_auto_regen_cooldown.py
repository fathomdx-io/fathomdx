"""Regression tests for auto_regen._within_cooldown's fail-safe path.

Before the fix, a corrupt `created_at` on the latest crystal delta
silently returned "not in cooldown" and let a regen fire. The
2026-04-19 runaway-regen incident was exactly this shape: one bad
crystal caused the NEXT tick to fire another one. The fail-safe must
be "treat as within cooldown" when we can't determine cooldown state
from the timestamp.
"""
from __future__ import annotations

import pytest

from api import auto_regen, crystal


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    # _last_fired_at is a module-global; clear before each test so one
    # test's "recent fire" doesn't mask another test's lake-path check.
    auto_regen._last_fired_at = None

    # Force a large cooldown so the "< cooldown" branch is the focus.
    monkeypatch.setattr(auto_regen.settings, "crystal_regen_cooldown_seconds", 10000)


@pytest.mark.asyncio
async def test_within_cooldown_on_unparseable_created_at(monkeypatch) -> None:
    """The corrupt-timestamp branch must fail safe (return True)."""
    async def _bad(*_args, **_kwargs):
        return {"created_at": "not-a-timestamp"}

    monkeypatch.setattr(crystal, "latest", _bad)
    assert await auto_regen._within_cooldown() is True


@pytest.mark.asyncio
async def test_within_cooldown_on_lake_unreachable(monkeypatch) -> None:
    """Separate fail-safe branch: crystal.latest raising must also map to
    within-cooldown = True (don't fire a regen against a dead lake)."""
    async def _raise(*_args, **_kwargs):
        raise RuntimeError("lake down")

    monkeypatch.setattr(crystal, "latest", _raise)
    assert await auto_regen._within_cooldown() is True


@pytest.mark.asyncio
async def test_within_cooldown_returns_false_when_outside_window(monkeypatch) -> None:
    """Happy path: the timestamp is parseable and older than cooldown →
    NOT within cooldown (regen is allowed)."""
    from datetime import UTC, datetime, timedelta

    old_ts = (datetime.now(UTC) - timedelta(days=365)).isoformat()

    async def _old(*_args, **_kwargs):
        return {"created_at": old_ts}

    monkeypatch.setattr(crystal, "latest", _old)
    assert await auto_regen._within_cooldown() is False


@pytest.mark.asyncio
async def test_within_cooldown_returns_true_for_recent_crystal(monkeypatch) -> None:
    """Happy path: the timestamp is recent → within cooldown (skip regen)."""
    from datetime import UTC, datetime, timedelta

    recent_ts = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()

    async def _recent(*_args, **_kwargs):
        return {"created_at": recent_ts}

    monkeypatch.setattr(crystal, "latest", _recent)
    assert await auto_regen._within_cooldown() is True
