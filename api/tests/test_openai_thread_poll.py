"""Tests for the OpenAI surface's thread-poll path.

Pins `_poll_thread_assistant`'s contract: query the lake for thread
assistant messages addressing the given user-msg-id, return new
(timestamp, content) tuples, mutate seen_ids to dedupe across calls.

The full `_await_witness_reply` is integration-shaped (involves
asyncio sleep loops), so we focus tests on the deterministic
helper plus the joining utility.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api import server

# ── _poll_thread_assistant ────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_thread_returns_empty_for_no_user_msg_id():
    """No user_msg_id → no polling, return empty."""
    with patch.object(server.delta_client, "query", AsyncMock(return_value=[])) as q:
        out = await server._poll_thread_assistant(
            user_msg_id="",
            after_iso="2026-05-04T12:00:00+00:00",
            seen_ids=set(),
        )
    assert out == []
    q.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_thread_filters_seen_and_old():
    """Already-seen ids and timestamps at-or-before after_iso are skipped."""
    rows = [
        {
            "id": "asst-already-seen",
            "timestamp": "2026-05-04T12:01:00+00:00",
            "content": "old reply",
        },
        {
            "id": "asst-too-old",
            "timestamp": "2026-05-04T12:00:00+00:00",  # equal to after_iso
            "content": "boundary case",
        },
        {
            "id": "asst-fresh",
            "timestamp": "2026-05-04T12:02:00+00:00",
            "content": "the new reply",
        },
    ]
    seen: set[str] = {"asst-already-seen"}
    with patch.object(server.delta_client, "query", AsyncMock(return_value=rows)):
        out = await server._poll_thread_assistant(
            user_msg_id="user-123",
            after_iso="2026-05-04T12:00:00+00:00",
            seen_ids=seen,
        )
    assert out == [("2026-05-04T12:02:00+00:00", "the new reply")]
    # seen_ids mutated to track the newly returned one
    assert "asst-fresh" in seen


@pytest.mark.asyncio
async def test_poll_thread_skips_empty_content():
    """Assistant message with no body shouldn't show up in the
    streamed response — we'd be telling the OpenAI client 'the
    assistant said nothing.' Better to silently skip and let the
    next non-empty reply (or timeout) close the request."""
    rows = [
        {"id": "asst-empty", "timestamp": "2026-05-04T12:01:00+00:00", "content": ""},
        {"id": "asst-blank", "timestamp": "2026-05-04T12:01:30+00:00", "content": "   "},
    ]
    with patch.object(server.delta_client, "query", AsyncMock(return_value=rows)):
        out = await server._poll_thread_assistant(
            user_msg_id="user-1",
            after_iso="2026-05-04T12:00:00+00:00",
            seen_ids=set(),
        )
    assert out == []


@pytest.mark.asyncio
async def test_poll_thread_lake_failure_returns_empty():
    """Lake hiccup must not crash the poller — return empty so the
    outer loop keeps trying."""
    with patch.object(
        server.delta_client,
        "query",
        AsyncMock(side_effect=Exception("lake down")),
    ):
        out = await server._poll_thread_assistant(
            user_msg_id="user-1",
            after_iso="2026-05-04T12:00:00+00:00",
            seen_ids=set(),
        )
    assert out == []


@pytest.mark.asyncio
async def test_poll_thread_query_uses_full_tag_set():
    """The query must filter on all three tags so we don't accidentally
    pick up unrelated thread-msg deltas (other user messages, etc.)."""
    captured: dict = {}

    async def fake_query(**kwargs):
        captured.update(kwargs)
        return []

    with patch.object(server.delta_client, "query", side_effect=fake_query):
        await server._poll_thread_assistant(
            user_msg_id="abc-123",
            after_iso="2026-05-04T12:00:00+00:00",
            seen_ids=set(),
        )
    tags = captured["tags_include"]
    assert "kind:thread-msg" in tags
    assert "role:assistant" in tags
    assert "addresses:abc-123" in tags


# ── _join_chronological ───────────────────────────────────────────


def test_join_chronological_orders_by_timestamp():
    """Two reply sources may interleave — timestamp order is the
    canonical render order so the OpenAI client reads what
    chronologically happened."""
    parts = [
        ("2026-05-04T12:02:00+00:00", "second"),
        ("2026-05-04T12:01:00+00:00", "first"),
        ("2026-05-04T12:03:00+00:00", "third"),
    ]
    out = server._join_chronological(parts)
    assert out == "first\n\nsecond\n\nthird"


def test_join_chronological_empty_returns_empty_string():
    assert server._join_chronological([]) == ""


def test_join_chronological_skips_empty_bodies():
    parts = [
        ("2026-05-04T12:01:00+00:00", "real"),
        ("2026-05-04T12:02:00+00:00", ""),
    ]
    assert server._join_chronological(parts) == "real"
