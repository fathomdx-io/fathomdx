"""Tests for the harness supervisor.

Pins the activation rule (fire when unaddressed > 0), the anti-spin
cap (don't burn CPU on a queue that isn't shrinking), and crash
safety (lake errors / fire errors / cancellation don't kill the
supervisor).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from api.loop import threaded_supervisor as ts


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
    with (
        patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)),
        patch.object(ts, "run_threaded_fire", AsyncMock(side_effect=fake_fire)),
    ):
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
    with (
        patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)),
        patch.object(ts, "run_threaded_fire", AsyncMock(side_effect=fake_fire)),
    ):
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
    with (
        patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)),
        patch.object(ts, "run_threaded_fire", AsyncMock(side_effect=fake_fire)),
    ):
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
    with (
        patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)),
        patch.object(ts, "run_threaded_fire", AsyncMock(side_effect=fake_fire)),
    ):
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
    with patch.object(
        ts.thread_mod, "build_window", AsyncMock(return_value={"messages": [], "unaddressed": []})
    ):
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
    monkeypatch.setattr(ts, "IDLE_POLL_S", 5.0)
    prev_task = ts._supervisor_task
    try:
        ts._supervisor_task = None
        with patch.object(
            ts.thread_mod,
            "build_window",
            AsyncMock(return_value={"messages": [], "unaddressed": []}),
        ):
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
    monkeypatch.setattr(ts, "IDLE_POLL_S", 5.0)
    prev_task = ts._supervisor_task
    try:
        ts._supervisor_task = None
        with patch.object(
            ts.thread_mod,
            "build_window",
            AsyncMock(return_value={"messages": [], "unaddressed": []}),
        ):
            ts.start()
            first = ts._supervisor_task
            ts.start()
            assert ts._supervisor_task is first
            await ts.stop()
    finally:
        ts._supervisor_task = prev_task


# ── self-continuation integration ─────────────────────────────────


@pytest.mark.asyncio
async def test_continuation_msg_picked_up_by_supervisor():
    """A continuation message written by `_maybe_continue_inquiry`
    lands as `role:user msg-kind:self-continue` with no tally mark —
    `thread.unaddressed()` must treat it as a pending msg so the
    supervisor's next poll fires fire-N+1."""
    from api import thread as thread_mod
    from api.loop.harness import threaded

    written: list[dict] = []

    async def fake_append(*, role, msg_kind, content, **kwargs):
        # Mirror what thread.append actually returns — a delta dict
        # with id, role-tag, msg-kind tag, no tally mark.
        d = {
            "id": f"d_{len(written):03d}",
            "content": content,
            "tags": [
                "kind:thread-msg",
                f"role:{role}",
                f"msg-kind:{msg_kind}",
                *(kwargs.get("extra_tags") or []),
            ],
            "timestamp": "2026-05-07T19:00:00Z",
        }
        written.append(d)
        return d

    # The continuation hook writes via thread_mod.append; the supervisor
    # reads via thread_mod.unaddressed. Both go through the same module.
    with patch.object(threaded.thread_mod, "append", side_effect=fake_append):
        await threaded._maybe_continue_inquiry(
            pending=[],
            addressed=[],
            body="fire-1 reply body",
            next_prompt="dig deeper on Y",
        )

    assert len(written) == 1
    cont = written[0]
    # The continuation msg is role:user — supervisor's unaddressed
    # filter (`thread._role(d) == "user"`) will include it.
    assert "role:user" in cont["tags"]
    # No addresses tag points at this msg id, so the tally check
    # treats it as unaddressed.
    assert not any(t.startswith("addresses:") for t in cont["tags"])

    # Simulate supervisor read: pending should include this msg.
    # `unaddressed` queries the lake for tally marks; mock to empty.
    with patch.object(thread_mod.delta_client, "query", AsyncMock(return_value=[])):
        pending = await thread_mod.unaddressed([cont])
    assert len(pending) == 1
    assert pending[0]["id"] == cont["id"]


# ── llm-down probe gate ───────────────────────────────────────────


@pytest.fixture
def _reset_probe_state():
    """Each gate test starts with a clean probe budget + cache."""
    from api.loop import llm_gate
    llm_gate.invalidate_cache()
    llm_gate.reset_probe_budget()
    yield
    llm_gate.invalidate_cache()
    llm_gate.reset_probe_budget()


@pytest.mark.asyncio
async def test_supervisor_skips_fire_when_hard_down_and_probe_budget_consumed(
    monkeypatch, _reset_probe_state,
):
    """While hard tier is down, the supervisor probes at most once per
    probe interval — held messages stay unaddressed until recovery."""
    from api.loop import llm_gate

    fire_count = 0
    pending = [_user_msg("u1")]

    async def fake_window():
        return {"messages": pending, "unaddressed": list(pending)}

    async def fake_fire():
        nonlocal fire_count
        fire_count += 1
        # Probe failed — message stays pending (would be a fresh error).
        return {"turns": 0, "addressed": [], "final_response": None, "lake_id": ""}

    monkeypatch.setattr(ts, "IDLE_POLL_S", 0.01)
    monkeypatch.setattr(ts, "BUSY_GAP_S", 0.005)
    # Make probe budget effectively eternal for this test so only the
    # first call returns True; subsequent calls within the test window
    # all return False.
    monkeypatch.setattr(llm_gate, "LLM_PROBE_INTERVAL_S", 60.0)
    with (
        patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)),
        patch.object(ts, "run_threaded_fire", AsyncMock(side_effect=fake_fire)),
        patch.object(llm_gate, "is_down", AsyncMock(return_value=True)),
    ):
        task = asyncio.create_task(ts._supervisor_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # Hard down + 60s budget → exactly one probe fire across many ticks.
    assert fire_count == 1, f"expected one probe fire, got {fire_count}"


@pytest.mark.asyncio
async def test_supervisor_fires_normally_when_hard_up(monkeypatch, _reset_probe_state):
    """When the gate reports up, no probe-budget gating — fires drain
    the queue at normal cadence."""
    from api.loop import llm_gate

    fire_count = 0
    pending = [_user_msg("u1")]

    async def fake_window():
        return {"messages": pending, "unaddressed": list(pending)}

    async def fake_fire():
        nonlocal fire_count
        fire_count += 1
        pending.clear()
        return {"turns": 1, "addressed": ["u1"], "final_response": {"body": "x"}, "lake_id": "abc"}

    monkeypatch.setattr(ts, "IDLE_POLL_S", 0.01)
    monkeypatch.setattr(ts, "BUSY_GAP_S", 0.01)
    with (
        patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)),
        patch.object(ts, "run_threaded_fire", AsyncMock(side_effect=fake_fire)),
        patch.object(llm_gate, "is_down", AsyncMock(return_value=False)),
    ):
        task = asyncio.create_task(ts._supervisor_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert fire_count >= 1


@pytest.mark.asyncio
async def test_supervisor_invalidates_gate_cache_after_fire(monkeypatch, _reset_probe_state):
    """After each fire, the supervisor drops the gate cache so the next
    tick reads the fresh heartbeat (on success) or error (on failure)
    instead of stale state from before the fire."""
    from api.loop import llm_gate

    pending = [_user_msg("u1")]
    invalidate_calls = 0

    async def fake_window():
        return {"messages": pending, "unaddressed": list(pending)}

    async def fake_fire():
        pending.clear()
        return {"turns": 1, "addressed": ["u1"], "final_response": {"body": "x"}, "lake_id": "abc"}

    def fake_invalidate(tier=None):
        nonlocal invalidate_calls
        invalidate_calls += 1

    monkeypatch.setattr(ts, "IDLE_POLL_S", 0.01)
    monkeypatch.setattr(ts, "BUSY_GAP_S", 0.01)
    with (
        patch.object(ts.thread_mod, "build_window", AsyncMock(side_effect=fake_window)),
        patch.object(ts, "run_threaded_fire", AsyncMock(side_effect=fake_fire)),
        patch.object(llm_gate, "is_down", AsyncMock(return_value=False)),
        patch.object(llm_gate, "invalidate_cache", side_effect=fake_invalidate),
    ):
        task = asyncio.create_task(ts._supervisor_loop())
        await asyncio.sleep(0.06)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert invalidate_calls >= 1
