"""Regression: routine fires must produce a thread-msg per fire,
even when content + source + non-time tags repeat.

The lake's sequential-dedup at delta-store/deltas/store.py:106 skips
writes whose most-recent same-source-same-tags row has identical
content. routines.fire and relief._fire_single both produce
fixed-string content per fire (the routine prompt body / the tier
directive), so without a per-fire varying tag every write after the
first silently no-ops and the threaded supervisor never sees the
activation.

The fix is `fired-at:<iso>` in extra_tags. These tests pin that
shape at the call site so a future refactor can't regress it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# ── routines.fire ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_routines_fire_thread_append_includes_fired_at_tag():
    """Each call to routines.fire passes a unique fired-at:<iso>
    tag in extra_tags so the lake won't dedup repeat fires."""
    from api import routines as r_mod

    captured: list[dict] = []

    async def fake_thread_append(**kwargs):
        captured.append(kwargs)
        return {"id": "thread-x"}

    # Stub out everything fire() does EXCEPT the thread.append call —
    # we only care that it threads fired-at into extra_tags.
    fake_spec = {
        "meta": {"name": "Test Routine", "host": "test-host"},
        "body": "do the thing",
    }

    async def fake_get_spec(rid):
        return fake_spec

    async def fake_write_intent(**kwargs):
        return {"id": "intent-1"}

    async def fake_lake_write(**kwargs):
        return {"id": "tick-1"}

    with patch.object(r_mod, "get_latest_spec", side_effect=fake_get_spec), \
         patch("api.loop.intents.write_intent", side_effect=fake_write_intent), \
         patch.object(r_mod.delta_client, "write", side_effect=fake_lake_write), \
         patch("api.thread.append", side_effect=fake_thread_append):
        await r_mod.fire("test-routine")

    assert len(captured) == 1
    extras = captured[0]["extra_tags"]
    fired_at_tags = [t for t in extras if t.startswith("fired-at:")]
    assert len(fired_at_tags) == 1, f"expected one fired-at tag, got: {extras}"
    # fired-at value should be a valid-shaped ISO timestamp ending in Z
    ts = fired_at_tags[0].split(":", 1)[1]
    assert ts.endswith("Z"), f"fired-at value should be Z-suffixed ISO: {ts!r}"
    # Host tag preserved alongside.
    assert "host:test-host" in extras


@pytest.mark.asyncio
async def test_routines_fire_two_consecutive_calls_have_different_fired_at():
    """Two fires in a row must produce DIFFERENT fired-at values, so
    the lake sees them as distinct rows."""
    from api import routines as r_mod

    captured: list[dict] = []

    async def fake_thread_append(**kwargs):
        captured.append(kwargs)
        return {"id": "thread-x"}

    fake_spec = {"meta": {"name": "T", "host": ""}, "body": "x"}

    async def fake_get_spec(rid):
        return fake_spec

    with patch.object(r_mod, "get_latest_spec", side_effect=fake_get_spec), \
         patch("api.loop.intents.write_intent", AsyncMock(return_value={"id": "i"})), \
         patch.object(r_mod.delta_client, "write", AsyncMock(return_value={"id": "t"})), \
         patch("api.thread.append", side_effect=fake_thread_append):
        await r_mod.fire("test-routine")
        # Tiny sleep to ensure ms-resolution timestamps differ.
        import asyncio
        await asyncio.sleep(0.005)
        await r_mod.fire("test-routine")

    assert len(captured) == 2
    ts0 = next(t for t in captured[0]["extra_tags"] if t.startswith("fired-at:"))
    ts1 = next(t for t in captured[1]["extra_tags"] if t.startswith("fired-at:"))
    assert ts0 != ts1, f"two fires produced the same fired-at: {ts0!r}"


# ── relief._fire_single ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_relief_fire_single_thread_append_includes_fired_at_tag():
    """Same fix for relief._fire_single — pressure-tier directives are
    FIXED strings per tier, so without a fired-at the second tick of
    the same tier silently no-ops at the lake."""
    from api.loop import relief

    captured: list[dict] = []

    async def fake_thread_append(**kwargs):
        captured.append(kwargs)
        return {"id": "thread-x"}

    tier = next(t for t in relief.RELIEF_TIERS if t["engine"] == "single-fire")

    with patch("api.loop.relief.write_intent", AsyncMock(return_value={"id": "i"})), \
         patch("api.thread.append", side_effect=fake_thread_append):
        await relief._fire_single(tier, reason="pressure")

    assert len(captured) == 1
    extras = captured[0]["extra_tags"]
    fired_at_tags = [t for t in extras if t.startswith("fired-at:")]
    assert len(fired_at_tags) == 1, f"expected one fired-at tag, got: {extras}"
