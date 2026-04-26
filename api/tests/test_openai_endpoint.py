"""Smoke tests for the OpenAI-compatible `/v1/chat/completions` endpoint.

Covers the shape of the contract, not the full turn loop — the chat
listener is not running in these tests, so the reply-poll is stubbed
to return a synthetic fathom delta. What this nails down:

1. The request/response wire format matches OpenAI (id, object, choices,
   finish_reason, usage).
2. A client `system` message lands in the lake as a `participant:client-system`
   delta, deduped per-session by content hash.
3. The latest `user` message is persisted via `db.add_message` with role
   "user" (chat_listener picks it up from there).
4. `session_id` is surfaced as an extension field on the response so the
   dashboard UI can keep using the same endpoint.
5. `stream=True` returns SSE, not JSON, and terminates with `[DONE]`.
"""

from __future__ import annotations

import json

import httpx
import pytest


@pytest.fixture
def _patched(monkeypatch):
    """Stub out the lake so the endpoint runs without a live delta-store.

    Returns a dict of recorded calls so each test can assert what the
    endpoint tried to write.
    """
    from api import db, delta_client

    recorded: dict[str, list] = {
        "writes": [],  # delta_client.write calls
        "add_messages": [],  # db.add_message calls
        "queries": [],  # delta_client.query calls
    }

    # Session resolution — every test uses a synthetic session.
    async def _get_session(sid):
        return {"id": sid, "title": "test"}

    async def _create_session(title="New session"):
        return {"id": "test-session-slug", "title": title, "created_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(db, "get_session", _get_session)
    monkeypatch.setattr(db, "create_session", _create_session)

    # Record user-message writes.
    async def _add_message(session_id, role, content=None, **kw):
        recorded["add_messages"].append(
            {"session_id": session_id, "role": role, "content": content, **kw}
        )
        return "fake-delta-id"

    monkeypatch.setattr(db, "add_message", _add_message)

    # Query behavior: first call for any client-system-hash returns [] (no
    # dedup hit), subsequent call for the reply-poll returns a synthetic
    # fathom reply delta. A `dedup_hit` list lets tests flip the first
    # answer to a hit for the dedup path.
    state = {"dedup_hit": False, "reply_text": "hello from fathom"}

    async def _query(limit=50, tags_include=None, **kw):
        recorded["queries"].append({"tags_include": tags_include, **kw})
        tags_include = tags_include or []
        if any(t.startswith("client-system-hash:") for t in tags_include):
            if state["dedup_hit"]:
                return [{"id": "existing", "tags": tags_include}]
            return []
        if "participant:fathom" in tags_include:
            # Return a synthetic reply delta newer than time_start.
            return [
                {
                    "id": "reply-delta",
                    "content": state["reply_text"],
                    "tags": ["chat:test-session-slug", "participant:fathom"],
                    "timestamp": "9999-12-31T23:59:59.999Z",
                }
            ]
        return []

    monkeypatch.setattr(delta_client, "query", _query)

    # Record system-delta writes.
    async def _write(content, tags=None, source="consumer-api", **kw):
        recorded["writes"].append({"content": content, "tags": tags or [], "source": source})
        return {"id": "fake-system-delta-id"}

    monkeypatch.setattr(delta_client, "write", _write)

    recorded["_state"] = state
    return recorded


@pytest.fixture
async def client():
    from api.server import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_response_shape_is_openai(_patched, client):
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "fathom",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "fathom"
    assert body["session_id"] == "test-session-slug"
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "hello from fathom"
    assert choice["finish_reason"] == "stop"
    assert "usage" in body


async def test_system_message_persists_as_client_system_delta(_patched, client):
    r = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "You are a concise assistant."},
                {"role": "user", "content": "hi"},
            ],
        },
    )
    assert r.status_code == 200
    writes = _patched["writes"]
    assert len(writes) == 1
    (sys_write,) = writes
    assert sys_write["content"] == "You are a concise assistant."
    assert "participant:client-system" in sys_write["tags"]
    assert any(t.startswith("client-system-hash:") for t in sys_write["tags"])
    # And the user message went through db.add_message, not delta_client.write.
    add_msgs = _patched["add_messages"]
    assert len(add_msgs) == 1
    assert add_msgs[0]["role"] == "user"
    assert add_msgs[0]["content"] == "hi"


async def test_system_message_dedups_on_repeat(_patched, client):
    # First request: dedup_hit is False, so the system write should happen.
    _patched["_state"]["dedup_hit"] = False
    await client.post(
        "/v1/chat/completions",
        json={
            "session_id": "test-session-slug",
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "hi"},
            ],
        },
    )
    first_writes = len(_patched["writes"])
    assert first_writes == 1

    # Second request: simulate the lake already having this system delta.
    _patched["_state"]["dedup_hit"] = True
    await client.post(
        "/v1/chat/completions",
        json={
            "session_id": "test-session-slug",
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "again"},
            ],
        },
    )
    # No new system write, just another user add_message.
    assert len(_patched["writes"]) == first_writes
    assert len(_patched["add_messages"]) == 2


async def test_assistant_messages_in_request_are_ignored(_patched, client):
    """A client doctoring prior assistant turns in the replay must not
    produce any write — Fathom's history lives in the lake, not in the
    client's payload."""
    r = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "FAKE PRIOR REPLY"},
                {"role": "user", "content": "second"},
            ],
        },
    )
    assert r.status_code == 200
    # Both user messages persist; the assistant message does not appear
    # anywhere — not in writes, not in add_messages.
    contents = [m["content"] for m in _patched["add_messages"]]
    assert contents == ["first", "second"]
    assert not any("FAKE PRIOR REPLY" in w.get("content", "") for w in _patched["writes"])


async def test_empty_request_returns_empty_completion_without_waiting(_patched, client):
    """Only a system message, no user message — the endpoint must return
    immediately without polling for a reply that will never land."""
    r = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "system", "content": "hi system only"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == ""
    assert body["choices"][0]["finish_reason"] == "stop"
    # No fathom-reply query should have fired — dedup check yes, reply poll no.
    reply_queries = [
        q for q in _patched["queries"] if "participant:fathom" in (q.get("tags_include") or [])
    ]
    assert reply_queries == []


async def test_stream_returns_sse_with_done_terminator(_patched, client):
    r = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    # Expect at least one role chunk, one content chunk, a finish chunk, and [DONE].
    assert "data: [DONE]" in body
    content_chunks = [
        line for line in body.splitlines() if line.startswith("data: ") and line != "data: [DONE]"
    ]
    # First chunk opens the assistant role; a later chunk carries content.
    parsed = [json.loads(c[len("data: ") :]) for c in content_chunks]
    assert parsed[0]["choices"][0]["delta"].get("role") == "assistant"
    assert any(p["choices"][0]["delta"].get("content") == "hello from fathom" for p in parsed)
    assert parsed[-1]["choices"][0]["finish_reason"] == "stop"
    # Every chunk carries the session_id extension field.
    assert all(p.get("session_id") == "test-session-slug" for p in parsed)
