"""Tests for `loop_generate_chat` — native chat-completions shape.

Pins request structure and response normalization. The threaded
harness depends on this returning a clean assistant message dict
(content + optional tool_calls) regardless of provider quirks.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.loop import llm


def _fake_response(content=None, tool_calls=None):
    """Build a mock chat-completions response matching the SDK shape."""
    msg = MagicMock()
    # model_dump returns a dict matching what we want to project.
    out: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        out["tool_calls"] = tool_calls
    msg.model_dump.return_value = out
    choice = MagicMock(message=msg)
    return SimpleNamespace(choices=[choice])


@pytest.mark.asyncio
async def test_chat_passes_messages_and_returns_assistant_dict():
    captured: dict = {}
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=lambda **kw: (captured.update(kw), _fake_response(content="hi"))[1]
    )
    with patch.object(llm, "_resolve_client_and_model", return_value=(fake_client, "model-x")):
        out = await llm.loop_generate_chat(
            messages=[{"role": "user", "content": "hello"}],
        )
    assert captured["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["model"] == "model-x"
    assert "tools" not in captured
    assert out["role"] == "assistant"
    assert out["content"] == "hi"


@pytest.mark.asyncio
async def test_chat_passes_tools_and_tool_choice():
    captured: dict = {}
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=lambda **kw: (captured.update(kw), _fake_response(content=""))[1]
    )
    tools = [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}]
    with patch.object(llm, "_resolve_client_and_model", return_value=(fake_client, "model-x")):
        await llm.loop_generate_chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            tool_choice="auto",
        )
    assert captured["tools"] == tools
    assert captured["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_chat_normalizes_none_content_to_empty_string():
    """Some providers return content=None when only tool_calls fire —
    downstream code expects content to be a string."""
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=_fake_response(content=None))
    with patch.object(llm, "_resolve_client_and_model", return_value=(fake_client, "m")):
        out = await llm.loop_generate_chat(messages=[{"role": "user", "content": "hi"}])
    assert out["content"] == ""


@pytest.mark.asyncio
async def test_chat_preserves_tool_calls():
    tool_calls = [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "semantic", "arguments": '{"query":"foo"}'},
        }
    ]
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_fake_response(content=None, tool_calls=tool_calls)
    )
    with patch.object(llm, "_resolve_client_and_model", return_value=(fake_client, "m")):
        out = await llm.loop_generate_chat(messages=[{"role": "user", "content": "hi"}])
    assert out["tool_calls"] == tool_calls


@pytest.mark.asyncio
async def test_chat_empty_messages_raises():
    with pytest.raises(ValueError, match="messages must be non-empty"):
        await llm.loop_generate_chat(messages=[])


@pytest.mark.asyncio
async def test_chat_empty_choices_returns_empty_assistant():
    """Providers occasionally return zero choices on bad input — don't
    crash, return a valid empty assistant turn."""
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[]))
    with patch.object(llm, "_resolve_client_and_model", return_value=(fake_client, "m")):
        out = await llm.loop_generate_chat(messages=[{"role": "user", "content": "hi"}])
    assert out == {"role": "assistant", "content": ""}
