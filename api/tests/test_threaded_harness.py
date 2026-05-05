"""Tests for the threaded harness fire loop.

Pins the contract — projection of thread deltas to chat messages,
the system block shape, the tool-call iteration, the respond-terminal
behavior, mark_addressed accumulation, and final-message persistence.

The LLM call is mocked; we don't hit any provider in these tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.loop.harness import threaded


# ── helpers ───────────────────────────────────────────────────────


def _user_delta(*, mid: str, content: str = "hi", channel: str = "composer", ts: str = "2026-05-04T12:00:00+00:00"):
    return {
        "id": mid,
        "timestamp": ts,
        "tags": ["kind:thread-msg", "role:user", "msg-kind:composer", f"channel:{channel}"],
        "content": content,
    }


def _asst_delta(*, mid: str, content: str = "ok", ts: str = "2026-05-04T12:01:00+00:00"):
    return {
        "id": mid,
        "timestamp": ts,
        "tags": ["kind:thread-msg", "role:assistant", "msg-kind:chat-reply"],
        "content": content,
    }


def _llm_response(*, content: str = "", tool_calls: list[dict] | None = None) -> dict:
    out: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        out["tool_calls"] = tool_calls
    return out


def _tool_call(name: str, args: dict, tcid: str = "call_1") -> dict:
    import json as _json
    return {
        "id": tcid,
        "type": "function",
        "function": {"name": name, "arguments": _json.dumps(args)},
    }


# ── projection ────────────────────────────────────────────────────


def test_project_user_delta_includes_id_and_channel_prefix():
    d = _user_delta(mid="abcdef123456789", content="hello", channel="openai")
    out = threaded._project_delta(d)
    assert out["role"] == "user"
    assert out["content"].startswith("[id=abcdef123456")
    assert "openai" in out["content"]
    assert out["content"].endswith("hello")


def test_project_assistant_delta_passes_content_through():
    d = _asst_delta(mid="z", content="responding")
    out = threaded._project_delta(d)
    assert out == {"role": "assistant", "content": "responding"}


def test_project_skips_empty_content():
    d = _user_delta(mid="x", content="")
    assert threaded._project_delta(d) is None


def test_project_skips_missing_role():
    d = {"id": "x", "tags": ["kind:thread-msg"], "content": "hi"}
    assert threaded._project_delta(d) is None


# ── tool args parsing ─────────────────────────────────────────────


def test_parse_tool_args_unpacks_json_string():
    tc = _tool_call("semantic", {"query": "x", "depth": "deep"})
    assert threaded._parse_tool_args(tc) == {"query": "x", "depth": "deep"}


def test_parse_tool_args_handles_dict_arguments():
    tc = {"function": {"name": "semantic", "arguments": {"query": "x"}}}
    assert threaded._parse_tool_args(tc) == {"query": "x"}


def test_parse_tool_args_returns_empty_on_malformed_json():
    tc = {"function": {"name": "x", "arguments": "{not json"}}
    assert threaded._parse_tool_args(tc) == {}


def test_parse_tool_args_returns_empty_on_missing_arguments():
    tc = {"function": {"name": "x"}}
    assert threaded._parse_tool_args(tc) == {}


# ── tally render ──────────────────────────────────────────────────


def test_tally_render_lists_unaddressed():
    rows = [_user_delta(mid="msg1", content="first"), _user_delta(mid="msg2", content="second")]
    out = threaded._render_tally(rows)
    assert "msg1" in out
    assert "msg2" in out
    assert "first" in out
    assert "second" in out


def test_tally_render_empty_message():
    assert "queue empty" in threaded._render_tally([])


# ── system message ────────────────────────────────────────────────


def test_system_message_includes_standpoint_and_tally():
    msg = threaded._build_system_message(
        standpoint_text="(identity stub)",
        unaddressed=[_user_delta(mid="u1", content="ping")],
    )
    assert msg["role"] == "system"
    assert "Fathom" in msg["content"]
    assert "(identity stub)" in msg["content"]
    assert "u1" in msg["content"]


# ── fire: respond-only path ──────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_respond_terminal_persists_assistant_message():
    """Model calls `respond` immediately — fire ends, assistant lands
    in thread with the addresses list."""
    user_msg = _user_delta(mid="u123", content="what's up")
    appended: list[dict] = []

    async def fake_build_window(**kwargs):
        return {"messages": [user_msg], "unaddressed": [user_msg]}

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return {"id": "asst-lake-id"}

    fake_chat = AsyncMock(return_value=_llm_response(
        tool_calls=[_tool_call("respond", {"body": "hello back", "addresses": ["u123"]}, "call_resp")],
    ))

    with patch.object(threaded.thread_mod, "build_window", fake_build_window), \
         patch.object(threaded.thread_mod, "append", fake_append), \
         patch.object(threaded, "loop_generate_chat", fake_chat):
        result = await threaded.run_threaded_fire(standpoint_text_override="")

    assert result["final_response"] == {"body": "hello back", "addresses": ["u123"]}
    assert result["turns"] == 1
    assert result["lake_id"] == "asst-lake-id"
    assert len(appended) == 1
    assert appended[0]["role"] == "assistant"
    assert appended[0]["msg_kind"] == "chat-reply"
    assert appended[0]["content"] == "hello back"
    assert appended[0]["addresses"] == ["u123"]


# ── fire: tool round then respond ─────────────────────────────────


@pytest.mark.asyncio
async def test_fire_runs_tool_then_responds():
    """Model calls semantic search, gets results, then responds."""
    user_msg = _user_delta(mid="u1", content="what about X")

    async def fake_build_window(**kwargs):
        return {"messages": [user_msg], "unaddressed": [user_msg]}

    async def fake_append(**kwargs):
        return {"id": "asst-id"}

    async def fake_dispatch(*, name, args, session_tag=""):
        if name == "semantic":
            return "found three deltas about X"
        return "?"

    chat_responses = [
        _llm_response(tool_calls=[_tool_call("semantic", {"query": "X"}, "call_search")]),
        _llm_response(tool_calls=[_tool_call("respond", {"body": "X is foo bar"}, "call_resp")]),
    ]
    fake_chat = AsyncMock(side_effect=chat_responses)

    with patch.object(threaded.thread_mod, "build_window", fake_build_window), \
         patch.object(threaded.thread_mod, "append", fake_append), \
         patch.object(threaded.tool_schemas, "dispatch", fake_dispatch), \
         patch.object(threaded, "loop_generate_chat", fake_chat):
        result = await threaded.run_threaded_fire(standpoint_text_override="")

    assert result["turns"] == 2
    assert result["final_response"]["body"] == "X is foo bar"


# ── fire: mark_addressed accumulation ────────────────────────────


@pytest.mark.asyncio
async def test_fire_accumulates_addressed_ids_across_turns():
    """Each mark_addressed tool call adds to the addressed list; the
    final assistant message inherits it as default addresses."""
    user_msg_a = _user_delta(mid="msg-a", content="ping a")
    user_msg_b = _user_delta(mid="msg-b", content="ping b")

    async def fake_build_window(**kwargs):
        return {"messages": [user_msg_a, user_msg_b], "unaddressed": [user_msg_a, user_msg_b]}

    appended: list[dict] = []

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return {"id": "x"}

    async def fake_dispatch(*, name, args, session_tag=""):
        if name == "mark_addressed":
            return f"Marked {args['user_message_id'][:12]} addressed."
        return "?"

    chat_responses = [
        _llm_response(tool_calls=[_tool_call("mark_addressed", {"user_message_id": "msg-a"}, "c1")]),
        _llm_response(tool_calls=[_tool_call("mark_addressed", {"user_message_id": "msg-b"}, "c2")]),
        _llm_response(tool_calls=[_tool_call("respond", {"body": "addressed both"}, "c3")]),
    ]
    fake_chat = AsyncMock(side_effect=chat_responses)

    with patch.object(threaded.thread_mod, "build_window", fake_build_window), \
         patch.object(threaded.thread_mod, "append", fake_append), \
         patch.object(threaded.tool_schemas, "dispatch", fake_dispatch), \
         patch.object(threaded, "loop_generate_chat", fake_chat):
        result = await threaded.run_threaded_fire(standpoint_text_override="")

    assert set(result["addressed"]) == {"msg-a", "msg-b"}
    # respond didn't supply explicit addresses → defaults to accumulated set
    assert set(appended[0]["addresses"]) == {"msg-a", "msg-b"}


# ── fire: no tool_calls (implicit respond) ───────────────────────


@pytest.mark.asyncio
async def test_fire_treats_plain_content_as_implicit_respond():
    """If the model returns content with no tool_calls, treat as respond."""
    async def fake_build_window(**kwargs):
        return {"messages": [], "unaddressed": []}

    appended: list[dict] = []

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return {"id": "x"}

    fake_chat = AsyncMock(return_value=_llm_response(content="quietly observing"))

    with patch.object(threaded.thread_mod, "build_window", fake_build_window), \
         patch.object(threaded.thread_mod, "append", fake_append), \
         patch.object(threaded, "loop_generate_chat", fake_chat):
        result = await threaded.run_threaded_fire(standpoint_text_override="")

    assert result["final_response"]["body"] == "quietly observing"
    assert result["turns"] == 1


# ── fire: max tool turns cap ─────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_caps_runaway_tool_loop():
    """If the model keeps calling tools and never responds, the cap
    fires and the fire ends without a final response."""
    async def fake_build_window(**kwargs):
        return {"messages": [], "unaddressed": []}

    async def fake_dispatch(*, name, args, session_tag=""):
        return "result"

    # Keep returning the same tool_call forever — no respond ever.
    fake_chat = AsyncMock(return_value=_llm_response(
        tool_calls=[_tool_call("semantic", {"query": "x"})],
    ))

    with patch.object(threaded.thread_mod, "build_window", fake_build_window), \
         patch.object(threaded.thread_mod, "append", AsyncMock(return_value={"id": "x"})), \
         patch.object(threaded.tool_schemas, "dispatch", fake_dispatch), \
         patch.object(threaded, "loop_generate_chat", fake_chat):
        result = await threaded.run_threaded_fire(
            standpoint_text_override="",
            max_tool_turns=3,
        )

    assert result["turns"] == 3
    assert result["final_response"] is None


# ── fire: LLM crash returns error ────────────────────────────────


@pytest.mark.asyncio
async def test_fire_bridges_assistant_reply_to_puddle():
    """Phase 5d bridge — the threaded reply should also land in the
    puddle as a feed-card so the legacy dashboard sees it."""
    user_msg = _user_delta(mid="u1", content="hi")

    async def fake_build_window(**kwargs):
        return {"messages": [user_msg], "unaddressed": [user_msg]}

    async def fake_thread_append(**kwargs):
        return {"id": "asst-thread-id"}

    puddle_calls: list[dict] = []

    async def fake_puddle_write(**kwargs):
        puddle_calls.append(kwargs)
        return {"id": "puddle-x"}

    fake_chat = AsyncMock(return_value=_llm_response(
        tool_calls=[_tool_call("respond", {"body": "the threaded reply", "addresses": ["u1"]}, "c")],
    ))

    with patch.object(threaded.thread_mod, "build_window", fake_build_window), \
         patch.object(threaded.thread_mod, "append", fake_thread_append), \
         patch.object(threaded, "loop_generate_chat", fake_chat), \
         patch("api.loop.puddle.puddle.write", side_effect=fake_puddle_write):
        await threaded.run_threaded_fire(standpoint_text_override="")

    # Two writes expected: one harness-turn trace + one bridge card.
    bridges = [c for c in puddle_calls if "feed-card" in (c.get("tags") or [])]
    assert len(bridges) == 1
    bridge = bridges[0]
    # JSON content with the body field
    import json as _json
    payload = _json.loads(bridge["content"])
    assert payload["body"] == "the threaded reply"
    # Tag shape matches witness chat-reply
    tags = bridge["tags"]
    assert "feed-card" in tags
    assert "route:chat-reply" in tags
    assert "addresses:u1" in tags
    assert "lake-id:asst-thread-id" in tags
    # Source distinguishes from witness cards
    assert bridge["source"] == "harness-threaded"

    # Trace also lands per Phase 5 thinking-accordion support.
    traces = [c for c in puddle_calls if "kind:harness-turn" in (c.get("tags") or [])]
    assert len(traces) >= 1


@pytest.mark.asyncio
async def test_fire_bridge_failure_does_not_break_thread_persistence():
    """The puddle bridge is best-effort — a bridge failure mustn't
    drop the assistant message from the thread."""
    async def fake_build_window(**kwargs):
        return {"messages": [], "unaddressed": []}

    appended_to_thread: list[dict] = []

    async def fake_thread_append(**kwargs):
        appended_to_thread.append(kwargs)
        return {"id": "x"}

    async def boom(**kwargs):
        raise RuntimeError("puddle down")

    fake_chat = AsyncMock(return_value=_llm_response(
        tool_calls=[_tool_call("respond", {"body": "hi"}, "c")],
    ))

    with patch.object(threaded.thread_mod, "build_window", fake_build_window), \
         patch.object(threaded.thread_mod, "append", fake_thread_append), \
         patch.object(threaded, "loop_generate_chat", fake_chat), \
         patch("api.loop.puddle.puddle.write", side_effect=boom):
        result = await threaded.run_threaded_fire(standpoint_text_override="")

    # Thread append still happened despite puddle bridge failure.
    assert len(appended_to_thread) == 1
    assert result["lake_id"] == "x"


@pytest.mark.asyncio
async def test_fire_handles_llm_exception():
    """A provider error doesn't blow up the fire — it returns a clean
    no-response result with the error captured."""
    async def fake_build_window(**kwargs):
        return {"messages": [], "unaddressed": []}

    fake_chat = AsyncMock(side_effect=RuntimeError("provider down"))

    with patch.object(threaded.thread_mod, "build_window", fake_build_window), \
         patch.object(threaded.thread_mod, "append", AsyncMock(return_value={"id": "x"})), \
         patch.object(threaded, "loop_generate_chat", fake_chat):
        result = await threaded.run_threaded_fire(standpoint_text_override="")

    assert result["final_response"] is None
    assert "RuntimeError" in result.get("error", "")
