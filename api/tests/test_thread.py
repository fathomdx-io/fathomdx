"""Tests for the global thread store.

Pins the data shape (tag conventions, role validation, addresses
fan-out) and the read primitives (tail / unaddressed / build_window)
so surfaces and the harness can rely on a stable contract while
the puddle is being decommissioned.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from api import thread

# ── append: tag construction ──────────────────────────────────────


@pytest.mark.asyncio
async def test_append_writes_kind_role_msgkind_tags():
    """Every thread message carries kind:thread-msg + role:* + msg-kind:*."""
    captured: dict = {}

    async def fake_write(**kwargs):
        captured.update(kwargs)
        return {"id": "abc123", "timestamp": "2026-05-04T12:00:00+00:00"}

    with patch.object(thread.delta_client, "write", side_effect=fake_write):
        await thread.append(
            role="user",
            msg_kind="composer",
            content="hello",
        )
    tags = captured["tags"]
    assert "kind:thread-msg" in tags
    assert "role:user" in tags
    assert "msg-kind:composer" in tags


@pytest.mark.asyncio
async def test_append_routing_tags():
    """channel + correlation + contact must round-trip as tags."""
    captured: dict = {}

    async def fake_write(**kwargs):
        captured.update(kwargs)
        return {"id": "x"}

    with patch.object(thread.delta_client, "write", side_effect=fake_write):
        await thread.append(
            role="user",
            msg_kind="openai-chat",
            content="what's for dinner",
            channel="openai",
            correlation="session-A",
            contact="myra",
        )
    tags = captured["tags"]
    assert "channel:openai" in tags
    assert "correlation:session-A" in tags
    assert "contact:myra" in tags


@pytest.mark.asyncio
async def test_append_addresses_fanout():
    """`addresses` writes one tag per id; assistant turns can address several."""
    captured: dict = {}

    async def fake_write(**kwargs):
        captured.update(kwargs)
        return {"id": "x"}

    with patch.object(thread.delta_client, "write", side_effect=fake_write):
        await thread.append(
            role="assistant",
            msg_kind="chat-reply",
            content="responding to both",
            addresses=["user-msg-1", "user-msg-2"],
        )
    tags = captured["tags"]
    assert "addresses:user-msg-1" in tags
    assert "addresses:user-msg-2" in tags


@pytest.mark.asyncio
async def test_append_tool_call_metadata():
    """role=tool messages carry tool-call-id + tool-name."""
    captured: dict = {}

    async def fake_write(**kwargs):
        captured.update(kwargs)
        return {"id": "x"}

    with patch.object(thread.delta_client, "write", side_effect=fake_write):
        await thread.append(
            role="tool",
            msg_kind="tool-result",
            content="search returned 5 hits",
            tool_call_id="call_abc",
            tool_name="search",
        )
    tags = captured["tags"]
    assert "role:tool" in tags
    assert "tool-call-id:call_abc" in tags
    assert "tool-name:search" in tags


@pytest.mark.asyncio
async def test_append_rejects_invalid_role():
    """Roles outside the OpenAI shape are a programming error."""
    with pytest.raises(ValueError, match="role must be"):
        await thread.append(role="ghost", msg_kind="composer", content="hi")


@pytest.mark.asyncio
async def test_append_rejects_empty_msg_kind():
    """msg_kind is required — surfaces must declare what they are."""
    with pytest.raises(ValueError, match="msg_kind required"):
        await thread.append(role="user", msg_kind="", content="hi")


@pytest.mark.asyncio
async def test_append_dedupes_repeated_tags():
    """An address listed twice or an extra-tag matching a built-in
    shouldn't double up in the lake row."""
    captured: dict = {}

    async def fake_write(**kwargs):
        captured.update(kwargs)
        return {"id": "x"}

    with patch.object(thread.delta_client, "write", side_effect=fake_write):
        await thread.append(
            role="assistant",
            msg_kind="chat-reply",
            content="x",
            addresses=["dup-id", "dup-id"],
            extra_tags=["kind:thread-msg", "custom-tag"],
        )
    tags = captured["tags"]
    assert tags.count("addresses:dup-id") == 1
    assert tags.count("kind:thread-msg") == 1
    assert "custom-tag" in tags


# ── mark_addressed ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_addressed_writes_tally_delta():
    captured: dict = {}

    async def fake_write(**kwargs):
        captured.update(kwargs)
        return {"id": "tally-1"}

    with patch.object(thread.delta_client, "write", side_effect=fake_write):
        await thread.mark_addressed(user_message_id="user-msg-7", note="answered")
    assert "kind:tally-mark" in captured["tags"]
    assert "addresses:user-msg-7" in captured["tags"]
    assert "marked-by:harness" in captured["tags"]
    assert captured["content"] == "answered"


@pytest.mark.asyncio
async def test_mark_addressed_requires_id():
    with pytest.raises(ValueError, match="user_message_id required"):
        await thread.mark_addressed(user_message_id="")


# ── tail ──────────────────────────────────────────────────────────


def _msg(*, id_: str, role: str, content: str = "x", ts: str = "2026-05-04T12:00:00+00:00"):
    return {
        "id": id_,
        "timestamp": ts,
        "tags": ["kind:thread-msg", f"role:{role}", "msg-kind:test"],
        "content": content,
    }


@pytest.mark.asyncio
async def test_tail_returns_chronological():
    """Oldest-first ordering — the harness needs the thread in
    conversation order, not lake-newest-first."""
    rows = [
        _msg(id_="m1", role="user", ts="2026-05-04T12:00:00+00:00"),
        _msg(id_="m3", role="user", ts="2026-05-04T12:02:00+00:00"),
        _msg(id_="m2", role="user", ts="2026-05-04T12:01:00+00:00"),
    ]

    async def fake_query(**kwargs):
        return list(rows)

    with patch.object(thread.delta_client, "query", side_effect=fake_query):
        out = await thread.tail(limit=15)
    assert [r["id"] for r in out] == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_tail_respects_message_limit():
    rows = [_msg(id_=f"m{i}", role="user", ts=f"2026-05-04T12:{i:02d}:00+00:00") for i in range(20)]

    async def fake_query(**kwargs):
        return list(rows)

    with patch.object(thread.delta_client, "query", side_effect=fake_query):
        out = await thread.tail(limit=5)
    assert len(out) == 5
    assert out[0]["id"] == "m15"
    assert out[-1]["id"] == "m19"


@pytest.mark.asyncio
async def test_tail_token_budget_trims_head():
    """When token budget is tight, the oldest messages drop first."""
    big = "x" * 4000  # ~1000 tokens at 4 chars/token + 20 overhead = ~1020
    rows = [
        _msg(id_=f"m{i}", role="user", content=big, ts=f"2026-05-04T12:{i:02d}:00+00:00")
        for i in range(5)
    ]

    async def fake_query(**kwargs):
        return list(rows)

    with patch.object(thread.delta_client, "query", side_effect=fake_query):
        out = await thread.tail(limit=15, max_tokens=2500)
    # 2500 / ~1020 per msg ≈ 2 messages fit
    assert 1 <= len(out) <= 3
    # Whatever we kept must be the most recent
    if len(out) >= 2:
        assert out[-1]["id"] == "m4"


# ── unaddressed ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unaddressed_filters_user_role_only():
    rows = [
        _msg(id_="u1", role="user"),
        _msg(id_="a1", role="assistant"),
        _msg(id_="t1", role="tool"),
        _msg(id_="u2", role="user"),
    ]

    async def fake_query(**kwargs):
        # Pretend no tally marks exist.
        if "kind:tally-mark" in (kwargs.get("tags_include") or []):
            return []
        return list(rows)

    with patch.object(thread.delta_client, "query", side_effect=fake_query):
        pending = await thread.unaddressed(rows)
    assert [r["id"] for r in pending] == ["u1", "u2"]


@pytest.mark.asyncio
async def test_unaddressed_excludes_marked():
    rows = [
        _msg(id_="u1", role="user"),
        _msg(id_="u2", role="user"),
        _msg(id_="u3", role="user"),
    ]
    marks = [
        {"id": "tally-1", "tags": ["kind:tally-mark", "addresses:u1"]},
        {"id": "tally-2", "tags": ["kind:tally-mark", "addresses:u3"]},
    ]

    async def fake_query(**kwargs):
        if "kind:tally-mark" in (kwargs.get("tags_include") or []):
            return list(marks)
        return list(rows)

    with patch.object(thread.delta_client, "query", side_effect=fake_query):
        pending = await thread.unaddressed(rows)
    # u1 and u3 marked → only u2 remains
    assert [r["id"] for r in pending] == ["u2"]


@pytest.mark.asyncio
async def test_unaddressed_empty_when_no_user_messages():
    rows = [_msg(id_="a1", role="assistant"), _msg(id_="t1", role="tool")]

    async def fake_query(**kwargs):
        return []

    with patch.object(thread.delta_client, "query", side_effect=fake_query):
        pending = await thread.unaddressed(rows)
    assert pending == []


# ── build_window ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_window_returns_messages_and_unaddressed():
    rows = [
        _msg(id_="u1", role="user", ts="2026-05-04T12:00:00+00:00"),
        _msg(id_="a1", role="assistant", ts="2026-05-04T12:01:00+00:00"),
        _msg(id_="u2", role="user", ts="2026-05-04T12:02:00+00:00"),
    ]
    marks = [{"id": "tally-1", "tags": ["kind:tally-mark", "addresses:u1"]}]

    async def fake_query(**kwargs):
        if "kind:tally-mark" in (kwargs.get("tags_include") or []):
            return list(marks)
        return list(rows)

    with patch.object(thread.delta_client, "query", side_effect=fake_query):
        window = await thread.build_window(max_messages=15)
    assert [m["id"] for m in window["messages"]] == ["u1", "a1", "u2"]
    assert [m["id"] for m in window["unaddressed"]] == ["u2"]
