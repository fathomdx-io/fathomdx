"""Tests for the threaded harness tool schemas + dispatch.

Pins the OpenAI function-calling schema shape (so the SDK accepts it),
the mark_addressed wiring through to thread.mark_addressed, and the
bridge to legacy tool handlers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.loop.harness import tool_schemas


# ── chat_tools shape ──────────────────────────────────────────────


def test_chat_tools_returns_function_objects():
    tools = tool_schemas.chat_tools()
    assert len(tools) >= 4
    for t in tools:
        assert t["type"] == "function"
        assert "name" in t["function"]
        assert "description" in t["function"]
        assert "parameters" in t["function"]
        params = t["function"]["parameters"]
        assert params["type"] == "object"
        assert "properties" in params


def test_mark_addressed_schema_present():
    names = {t["function"]["name"] for t in tool_schemas.chat_tools()}
    assert "mark_addressed" in names
    assert "respond" in names
    # Sanity — bridge tools are also exposed.
    assert "semantic" in names
    assert "dispatch_helper" in names


def test_tool_names_matches_schemas():
    schema_names = {t["function"]["name"] for t in tool_schemas.chat_tools()}
    assert tool_schemas.tool_names() == schema_names


def test_returns_fresh_dicts_each_call():
    """Caller may mutate the list; subsequent calls shouldn't see it."""
    a = tool_schemas.chat_tools()
    a.append({"type": "function", "function": {"name": "extra"}})
    b = tool_schemas.chat_tools()
    assert "extra" not in {t["function"]["name"] for t in b}


# ── mark_addressed handler ────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_addressed_calls_thread_module():
    with patch.object(
        tool_schemas.thread,
        "mark_addressed",
        AsyncMock(return_value={"id": "tally-id-12345"}),
    ) as mock_mark:
        out = await tool_schemas.tool_mark_addressed(
            user_message_id="user-msg-123456",
            note="answered inline",
        )
    mock_mark.assert_awaited_once_with(
        user_message_id="user-msg-123456",
        note="answered inline",
    )
    assert "user-msg-123" in out
    assert "tally-id-123" in out


@pytest.mark.asyncio
async def test_mark_addressed_empty_id_returns_error():
    out = await tool_schemas.tool_mark_addressed(user_message_id="")
    assert out.startswith("ERROR")


@pytest.mark.asyncio
async def test_mark_addressed_lake_failure_returns_error_string():
    """Tool errors must NOT raise — they return strings the model can read."""
    with patch.object(
        tool_schemas.thread,
        "mark_addressed",
        AsyncMock(side_effect=Exception("lake down")),
    ):
        out = await tool_schemas.tool_mark_addressed(user_message_id="x123")
    assert out.startswith("ERROR")
    assert "lake down" in out


# ── dispatch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_routes_mark_addressed():
    with patch.object(
        tool_schemas.thread,
        "mark_addressed",
        AsyncMock(return_value={"id": "tally-x"}),
    ):
        out = await tool_schemas.dispatch(
            name="mark_addressed",
            args={"user_message_id": "abc"},
        )
    assert "Marked abc" in out


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error():
    out = await tool_schemas.dispatch(name="not_a_tool", args={})
    assert out.startswith("ERROR")
    assert "unknown tool" in out


@pytest.mark.asyncio
async def test_dispatch_respond_returns_misuse_error():
    """`respond` is the loop-driver's terminal signal — if it reaches
    dispatch, that's a bug in the loop, not a tool to run."""
    out = await tool_schemas.dispatch(name="respond", args={"body": "x"})
    assert out.startswith("ERROR")
    assert "respond" in out


@pytest.mark.asyncio
async def test_dispatch_filters_args_by_tool_model_args():
    """Stray kwargs from the model don't leak into the handler."""
    captured: dict = {}

    async def fake_search(**kwargs):
        captured.update(kwargs)
        return "search result"

    from api.loop.harness import tools as legacy_tools

    with patch.dict(legacy_tools.TOOL_HANDLERS, {"semantic": fake_search}):
        out = await tool_schemas.dispatch(
            name="semantic",
            args={"query": "hello", "depth": "deep", "extra_kwarg": "ignored"},
        )
    assert out == "search result"
    assert "extra_kwarg" not in captured
    assert captured == {"query": "hello", "depth": "deep"}


@pytest.mark.asyncio
async def test_dispatch_handler_exception_returns_error_string():
    async def boom(**kwargs):
        raise RuntimeError("tool blew up")

    from api.loop.harness import tools as legacy_tools

    with patch.dict(legacy_tools.TOOL_HANDLERS, {"semantic": boom}):
        out = await tool_schemas.dispatch(name="semantic", args={"query": "x"})
    assert out.startswith("ERROR")
    assert "tool blew up" in out
