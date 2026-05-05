"""Tests for the /v1/thread/{window,unaddressed} endpoints.

Pins the projection from kind:thread-msg lake delta → UI-friendly
dict, the chronological ordering of the window, and the unaddressed
filter's preview shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.loop import routes as loop_routes


# ── _project_thread_msg ───────────────────────────────────────────


def test_project_pulls_role_and_msg_kind_to_top_level():
    d = {
        "id": "abc",
        "timestamp": "2026-05-04T12:00:00+00:00",
        "tags": ["kind:thread-msg", "role:user", "msg-kind:openai-chat"],
        "content": "hello",
        "source": "openai-compat",
    }
    out = loop_routes._project_thread_msg(d)
    assert out["id"] == "abc"
    assert out["role"] == "user"
    assert out["msg_kind"] == "openai-chat"
    assert out["content"] == "hello"
    assert out["source"] == "openai-compat"


def test_project_includes_routing_fields():
    d = {
        "id": "x",
        "tags": [
            "kind:thread-msg",
            "role:user",
            "msg-kind:openai-chat",
            "channel:openai",
            "correlation:session-A",
            "contact:myra",
        ],
        "content": "hi",
    }
    out = loop_routes._project_thread_msg(d)
    assert out["channel"] == "openai"
    assert out["correlation"] == "session-A"
    assert out["contact"] == "myra"


def test_project_collects_addresses_list_in_order_dedup():
    d = {
        "id": "x",
        "tags": [
            "kind:thread-msg",
            "role:assistant",
            "msg-kind:chat-reply",
            "addresses:user-1",
            "addresses:user-2",
            "addresses:user-1",  # duplicate — must dedupe
        ],
        "content": "responding to two",
    }
    out = loop_routes._project_thread_msg(d)
    assert out["addresses"] == ["user-1", "user-2"]


def test_project_empty_optional_fields_are_empty_string():
    d = {
        "id": "x",
        "tags": ["kind:thread-msg", "role:user", "msg-kind:composer"],
        "content": "hi",
    }
    out = loop_routes._project_thread_msg(d)
    assert out["channel"] == ""
    assert out["correlation"] == ""
    assert out["contact"] == ""
    assert out["addresses"] == []


# ── /v1/thread/window ─────────────────────────────────────────────


def _msg(*, mid: str, role: str, content: str = "x", ts: str = "2026-05-04T12:00:00+00:00", extra_tags=None):
    tags = ["kind:thread-msg", f"role:{role}", "msg-kind:test"]
    tags.extend(extra_tags or [])
    return {"id": mid, "timestamp": ts, "tags": tags, "content": content}


@pytest.mark.asyncio
async def test_window_returns_projected_messages():
    rows = [_msg(mid="m1", role="user"), _msg(mid="m2", role="assistant")]
    with patch("api.thread.tail", AsyncMock(return_value=rows)):
        out = await loop_routes.get_thread_window()
    assert out["count"] == 2
    assert out["messages"][0]["role"] == "user"
    assert out["messages"][1]["role"] == "assistant"
    assert out["window_limit"] == 15
    assert out["max_tokens"] == 12_000


@pytest.mark.asyncio
async def test_window_passes_through_query_params():
    captured: dict = {}

    async def fake_tail(**kwargs):
        captured.update(kwargs)
        return []

    with patch("api.thread.tail", side_effect=fake_tail):
        out = await loop_routes.get_thread_window(limit=5, max_tokens=2000)
    assert captured == {"limit": 5, "max_tokens": 2000}
    assert out["window_limit"] == 5
    assert out["max_tokens"] == 2000


@pytest.mark.asyncio
async def test_window_empty():
    with patch("api.thread.tail", AsyncMock(return_value=[])):
        out = await loop_routes.get_thread_window()
    assert out["messages"] == []
    assert out["count"] == 0


# ── /v1/thread/unaddressed ────────────────────────────────────────


@pytest.mark.asyncio
async def test_unaddressed_returns_pending_with_preview():
    long_body = "x" * 300
    rows = [_msg(mid="u1", role="user", content=long_body)]
    window = {"messages": rows, "unaddressed": rows}
    with patch("api.thread.build_window", AsyncMock(return_value=window)):
        out = await loop_routes.get_thread_unaddressed()
    assert out["count"] == 1
    item = out["unaddressed"][0]
    assert item["id"] == "u1"
    assert item["content"] == long_body
    assert len(item["preview"]) == 200


@pytest.mark.asyncio
async def test_unaddressed_empty_when_queue_empty():
    with patch("api.thread.build_window", AsyncMock(return_value={"messages": [], "unaddressed": []})):
        out = await loop_routes.get_thread_unaddressed()
    assert out == {"unaddressed": [], "count": 0}


@pytest.mark.asyncio
async def test_unaddressed_passes_through_window_params():
    captured: dict = {}

    async def fake_window(**kwargs):
        captured.update(kwargs)
        return {"messages": [], "unaddressed": []}

    with patch("api.thread.build_window", side_effect=fake_window):
        await loop_routes.get_thread_unaddressed(limit=8, max_tokens=4000)
    assert captured == {"max_messages": 8, "max_tokens": 4000}
