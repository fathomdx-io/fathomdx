"""Tests for legacy supervisor dormancy when threaded harness is on.

Pins the cutover gate: when FATHOM_THREADED_HARNESS=1, the legacy
supervisor sleeps each tick instead of firing. Without this, both
supervisors would fire on the same operator turn (each surface
shadow-writes to both puddle AND thread per Phase 1), doubling
LLM cost on every interaction.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from api.loop import worker


@pytest.mark.asyncio
async def test_legacy_supervisor_idles_when_threaded_flag_on(monkeypatch):
    """Threaded flag on → _run_one_fire never called."""
    fire_calls = 0

    async def fake_fire():
        nonlocal fire_calls
        fire_calls += 1
        return False

    monkeypatch.setenv("FATHOM_THREADED_HARNESS", "1")
    monkeypatch.setattr(worker, "IDLE_SLEEP_S", 0.005)
    with patch.object(worker, "_run_one_fire", side_effect=fake_fire):
        task = asyncio.create_task(worker._supervisor())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert fire_calls == 0


@pytest.mark.asyncio
async def test_legacy_supervisor_fires_when_threaded_flag_off(monkeypatch):
    """Threaded flag off → _run_one_fire IS called as normal."""
    fire_calls = 0

    async def fake_fire():
        nonlocal fire_calls
        fire_calls += 1
        return False  # nothing pending → idle path

    monkeypatch.delenv("FATHOM_THREADED_HARNESS", raising=False)
    monkeypatch.setattr(worker, "IDLE_SLEEP_S", 0.005)
    with patch.object(worker, "_run_one_fire", side_effect=fake_fire):
        task = asyncio.create_task(worker._supervisor())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert fire_calls >= 1


@pytest.mark.asyncio
async def test_legacy_supervisor_responds_to_flag_flip_mid_run(monkeypatch):
    """The flag is read each tick, so flipping it on mid-run should
    quiet the supervisor without restart."""
    fire_calls = 0

    async def fake_fire():
        nonlocal fire_calls
        fire_calls += 1
        return False

    monkeypatch.delenv("FATHOM_THREADED_HARNESS", raising=False)
    monkeypatch.setattr(worker, "IDLE_SLEEP_S", 0.005)
    with patch.object(worker, "_run_one_fire", side_effect=fake_fire):
        task = asyncio.create_task(worker._supervisor())
        await asyncio.sleep(0.03)
        before_count = fire_calls
        # Flip the flag on
        monkeypatch.setenv("FATHOM_THREADED_HARNESS", "1")
        await asyncio.sleep(0.06)
        after_count = fire_calls
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # First half fired (flag was off). Second half should NOT add many
    # more fires (flag flipped on; supervisor should be sleeping the
    # longer dormant interval).
    assert before_count >= 1
    # Allow some slop — the flag flip might race with one in-flight tick.
    assert after_count - before_count <= 2
