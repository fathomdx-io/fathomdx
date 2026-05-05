"""Tests for the threaded harness supervisor.

Pins the activation rule (fire when unaddressed > 0), the anti-spin
cap (don't burn CPU on a queue that isn't shrinking), the feature
flag gating (off → no fire), and crash safety (lake errors / fire
errors / cancellation don't kill the supervisor).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from api.loop import threaded_supervisor as ts


# ── feature flag ──────────────────────────────────────────────────


def test_is_enabled_false_by_default(monkeypatch):
    monkeypatch.delenv("FATHOM_THREADED_HARNESS", raising=False)
    assert ts.is_enabled() is False


def test_is_enabled_reads_env_fresh(monkeypatch):
    monkeypatch.setenv("FATHOM_THREADED_HARNESS", "1")
    assert ts.is_enabled() is True
    monkeypatch.setenv("FATHOM_THREADED_HARNESS", "0")
    assert ts.is_enabled() is False


def test_is_enabled_only_one_truthy_value(monkeypatch):
    """Sloppy values like 'true' or 'yes' don't enable — explicit '1' only."""
    for v in ("true", "yes", "on", "True", "TRUE"):
        monkeypatch.setenv("FATHOM_THREADED_HARNESS", v)
        assert ts.is_enabled() is False, f"value={v!r} should NOT enable"


def test_start_no_op_when_flag_off(monkeypatch):
    """start() is a no-op when the flag is off — never spawns the task."""
    monkeypatch.delenv("FATHOM_THREADED_HARNESS", raising=False)
    # Save and restore module state
    prev = ts._supervisor_task
    try:
        ts._supervisor_task = None
        ts.start()
        assert ts._supervisor_task is None
    finally:
        ts._supervisor_task = prev


# ── _supervisor_loop activation logic ─────────────────────────────


def _user_msg(mid: str = "u1"):
    return {"id": mid, "tags": ["kind:thread-msg", "role:user"], "content": "hi"}


@pytest.mark.asyncio
async def test_loop_skips_fire_when_no_pending(monkeypatch):
    """No unaddressed → no fire; loop just sleeps."""
    fire_count = 0

    async def fake_window():
        return {"messages": [], "unaddressed": []}

    async def fake_fire():
        nonlocal fire_count
        fire_count += 1
        return {"turns": 1, "addressed": [], "final_response": None, "lake_id": ""}

    # Run one iteration by setting a tiny poll, kicking the task,
    # then cancelling.
    monkeypatch.setattr(ts, "IDLE_POLL_S", 0.01)
    with patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)), \
         patch.object(ts, "run_threaded_fire", AsyncMock(side_effect=fake_fire)):
        task = asyncio.create_task(ts._supervisor_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert fire_count == 0


@pytest.mark.asyncio
async def test_loop_fires_when_pending_present(monkeypatch):
    """Unaddressed user msg → fire runs."""
    fire_count = 0
    pending = [_user_msg("u1")]

    async def fake_window():
        return {"messages": pending, "unaddressed": list(pending)}

    async def fake_fire():
        nonlocal fire_count
        fire_count += 1
        # Simulate addressing the message — empty the queue.
        pending.clear()
        return {"turns": 2, "addressed": ["u1"], "final_response": {"body": "x"}, "lake_id": "abc"}

    monkeypatch.setattr(ts, "IDLE_POLL_S", 0.01)
    monkeypatch.setattr(ts, "BUSY_GAP_S", 0.01)
    with patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)), \
         patch.object(ts, "run_threaded_fire", AsyncMock(side_effect=fake_fire)):
        task = asyncio.create_task(ts._supervisor_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert fire_count >= 1


@pytest.mark.asyncio
async def test_loop_anti_spin_caps_fire_rate(monkeypatch):
    """If the queue never shrinks, anti-spin keeps fire rate bounded
    by inserting long idle sleeps between bursts. Without it, fires
    would loop at BUSY_GAP_S — way above what we cap to.

    Math with these settings (cap=3, busy=0.005, idle=0.01,
    anti-spin sleep=4×idle=0.04):
      · without anti-spin: ~30 fires in 0.15s
      · with anti-spin: ~3 fires/burst, ~55ms between bursts → ≤12

    The exact count isn't pinned (asyncio scheduling variance), but
    it must be far below the unbounded case. Anti-spin's log line
    is also observable in the test output for verification.
    """
    fire_count = 0
    pending = [_user_msg("u1")]  # never gets cleared

    async def fake_window():
        return {"messages": pending, "unaddressed": list(pending)}

    async def fake_fire():
        nonlocal fire_count
        fire_count += 1
        return {"turns": 1, "addressed": [], "final_response": None, "lake_id": ""}

    monkeypatch.setattr(ts, "IDLE_POLL_S", 0.01)
    monkeypatch.setattr(ts, "BUSY_GAP_S", 0.005)
    monkeypatch.setattr(ts, "MAX_CONSECUTIVE_FIRES", 3)
    with patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)), \
         patch.object(ts, "run_threaded_fire", AsyncMock(side_effect=fake_fire)):
        task = asyncio.create_task(ts._supervisor_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # Without anti-spin we'd expect ~30 fires; cap holds us well below.
    assert 3 <= fire_count <= 12, f"fire_count={fire_count} out of expected range"


@pytest.mark.asyncio
async def test_loop_survives_window_read_error(monkeypatch):
    """A lake hiccup on build_window doesn't kill the supervisor."""
    call_count = 0

    async def fake_window():
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise RuntimeError("lake down")
        return {"messages": [], "unaddressed": []}

    monkeypatch.setattr(ts, "IDLE_POLL_S", 0.01)
    with patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)):
        task = asyncio.create_task(ts._supervisor_loop())
        await asyncio.sleep(0.08)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert call_count >= 3  # recovered after errors


@pytest.mark.asyncio
async def test_loop_survives_fire_error(monkeypatch):
    """A run_threaded_fire crash doesn't kill the supervisor."""
    fire_calls = 0
    pending = [_user_msg("u1")]

    async def fake_window():
        return {"messages": pending, "unaddressed": list(pending)}

    async def fake_fire():
        nonlocal fire_calls
        fire_calls += 1
        if fire_calls == 1:
            raise RuntimeError("fire crashed")
        pending.clear()
        return {"turns": 1, "addressed": ["u1"], "final_response": {"body": "x"}, "lake_id": "abc"}

    monkeypatch.setattr(ts, "IDLE_POLL_S", 0.01)
    monkeypatch.setattr(ts, "BUSY_GAP_S", 0.01)
    with patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)), \
         patch.object(ts, "run_threaded_fire", AsyncMock(side_effect=fake_fire)):
        task = asyncio.create_task(ts._supervisor_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert fire_calls >= 2  # crashed once, then succeeded


@pytest.mark.asyncio
async def test_loop_responds_to_cancellation(monkeypatch):
    """Cancellation during sleep returns cleanly."""
    monkeypatch.setattr(ts, "IDLE_POLL_S", 5.0)
    with patch.object(ts.thread_mod, "build_window", AsyncMock(return_value={"messages": [], "unaddressed": []})):
        task = asyncio.create_task(ts._supervisor_loop())
        await asyncio.sleep(0.02)
        task.cancel()
        # Should return cleanly, not raise.
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.CancelledError:
            pass
        assert task.done()


# ── start / stop lifecycle ────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_then_stop_cleans_up(monkeypatch):
    monkeypatch.setenv("FATHOM_THREADED_HARNESS", "1")
    monkeypatch.setattr(ts, "IDLE_POLL_S", 5.0)
    prev_task = ts._supervisor_task
    try:
        ts._supervisor_task = None
        with patch.object(ts.thread_mod, "build_window", AsyncMock(return_value={"messages": [], "unaddressed": []})):
            ts.start()
            assert ts._supervisor_task is not None
            assert not ts._supervisor_task.done()
            await ts.stop()
            assert ts._supervisor_task is None
    finally:
        ts._supervisor_task = prev_task


@pytest.mark.asyncio
async def test_start_idempotent(monkeypatch):
    """Calling start() twice doesn't spawn a second task."""
    monkeypatch.setenv("FATHOM_THREADED_HARNESS", "1")
    monkeypatch.setattr(ts, "IDLE_POLL_S", 5.0)
    prev_task = ts._supervisor_task
    try:
        ts._supervisor_task = None
        with patch.object(ts.thread_mod, "build_window", AsyncMock(return_value={"messages": [], "unaddressed": []})):
            ts.start()
            first = ts._supervisor_task
            ts.start()
            assert ts._supervisor_task is first
            await ts.stop()
    finally:
        ts._supervisor_task = prev_task
