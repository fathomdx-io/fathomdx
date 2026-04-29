"""Smoke tests for the OpenAI-compatible `/v1/chat/completions` endpoint.

The endpoint no longer runs `fathom_think` directly — it drops the
user's turn into the puddle as a `kind:question` intent tagged
`channel:openai` + `openai-session:<sid>`, then polls the lake for a
witness card tagged `to:openai:<sid>` + `addresses:<intent_id>`. These
tests stub the puddle and lake so the contract can be exercised
without a live Grand Loop.

What this nails down:

1. The request/response wire format matches OpenAI (id, object, choices,
   finish_reason, usage).
2. The latest `user` message becomes a single intent in the puddle with
   the correct channel / correlation tags.
3. Older user / assistant / system messages in the request are ignored
   — Fathom's history lives in the lake, not the client's replay.
4. `session_id` is surfaced as an extension field on the response so the
   dashboard UI can keep using the same endpoint.
5. `stream=True` returns SSE, not JSON, and terminates with `[DONE]`.
6. Concurrent sessions get their own replies (no cross-talk).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest


def _witness_card_content(body: str) -> str:
    """Witness writes JSON payloads — match that shape so the renderer
    extracts the body the way it does in production."""
    return json.dumps({
        "kicker": "",
        "title": "",
        "body": body,
        "tail": "",
        "route": "chat-reply",
        "axes": {},
    })


@pytest.fixture
def _patched(monkeypatch):
    """Stub the puddle (intent writer) and lake (witness output reader).

    Records every intent write and every witness-card poll so tests can
    assert tag shapes and routing scope.
    """
    from api import delta_client, db
    from api.loop import puddle as puddle_mod

    recorded: dict[str, list] = {
        "intents": [],   # puddle.write calls (write_intent → puddle.write)
        "queries": [],   # delta_client.query calls
        "writes": [],    # delta_client.write calls (any direct lake writes)
    }

    # Session resolution — every test uses a synthetic session.
    async def _get_session(sid):
        return {"id": sid, "title": "test"}

    async def _create_session(title="New session"):
        return {"id": "test-session-slug", "title": title, "created_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(db, "get_session", _get_session)
    monkeypatch.setattr(db, "create_session", _create_session)

    # Capture every intent write (the puddle is in-process, so we patch
    # the singleton's write method).
    intent_counter = {"n": 0}

    async def _puddle_write(*, content, tags, source, ttl_seconds=None, expires_at=None):
        intent_counter["n"] += 1
        intent = {
            "id": f"intent-{intent_counter['n']}",
            "content": content,
            "tags": list(tags),
            "source": source,
            "timestamp": "2026-01-01T00:00:00Z",
        }
        recorded["intents"].append(intent)
        return intent

    monkeypatch.setattr(puddle_mod.puddle, "write", _puddle_write)

    # By default reply with one witness card whenever the endpoint polls
    # for `to:openai:*`. Tests can override `reply_text` per-session via
    # `_state["replies"]`.
    state: dict = {
        "replies": {},  # session_id → reply text
        "default_reply": "hello from fathom",
    }

    async def _query(limit=50, tags_include=None, **kw):
        recorded["queries"].append({"tags_include": tags_include, **kw})
        tags_include = tags_include or []
        # Look for the to:openai:<sid> tag; that's the witness-output poll.
        for t in tags_include:
            if t.startswith("to:openai:"):
                sid = t.split(":", 2)[2]
                body = state["replies"].get(sid, state["default_reply"])
                return [
                    {
                        "id": f"witness-{sid}",
                        "content": _witness_card_content(body),
                        "tags": tags_include,
                        "timestamp": "9999-12-31T23:59:59.999Z",
                    }
                ]
        return []

    monkeypatch.setattr(delta_client, "query", _query)

    async def _write(content, tags=None, source="fathom-engagement", **kw):
        recorded["writes"].append({"content": content, "tags": tags or [], "source": source})
        return {"id": "fake-write-id"}

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


async def test_user_message_becomes_intent_with_channel_tags(_patched, client):
    await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "what's up"}]},
    )
    intents = _patched["intents"]
    assert len(intents) == 1
    intent = intents[0]
    assert intent["content"] == "what's up"
    tags = intent["tags"]
    assert "intent" in tags
    assert "kind:question" in tags
    assert "channel:openai" in tags
    assert "openai-session:test-session-slug" in tags


async def test_system_and_assistant_messages_in_request_are_ignored(_patched, client):
    """Only the latest user message becomes an intent. System messages
    and prior assistant turns are inert — Fathom orients from the lake."""
    r = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "FAKE PRIOR REPLY"},
                {"role": "user", "content": "second"},
            ],
        },
    )
    assert r.status_code == 200
    intents = _patched["intents"]
    # Only one intent — the LAST user message.
    assert len(intents) == 1
    assert intents[0]["content"] == "second"
    # The system / assistant content never appears as an intent.
    all_intent_content = " ".join(i["content"] for i in intents)
    assert "Be concise." not in all_intent_content
    assert "FAKE PRIOR REPLY" not in all_intent_content
    assert "first" not in all_intent_content


async def test_empty_request_returns_empty_completion_without_waiting(_patched, client):
    """No user message — endpoint returns immediately without writing an
    intent or polling for a reply."""
    r = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "system", "content": "system only"}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == ""
    assert body["choices"][0]["finish_reason"] == "stop"
    # No intent written; no witness poll fired.
    assert _patched["intents"] == []
    poll_queries = [
        q for q in _patched["queries"]
        if any(str(t).startswith("to:openai:") for t in (q.get("tags_include") or []))
    ]
    assert poll_queries == []


async def test_reply_poll_is_scoped_to_intent_id(_patched, client):
    """The witness-output poll must filter by both `to:openai:<sid>` AND
    `addresses:<intent_id>` — without the intent_id scope a later request
    in the same session would pick up an earlier reply."""
    await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    poll_queries = [
        q for q in _patched["queries"]
        if any(str(t).startswith("to:openai:") for t in (q.get("tags_include") or []))
    ]
    assert poll_queries, "expected at least one witness-output poll"
    tags = poll_queries[0]["tags_include"]
    assert "to:openai:test-session-slug" in tags
    assert any(t.startswith("addresses:") for t in tags)


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
    assert "data: [DONE]" in body
    content_chunks = [
        line for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    parsed = [json.loads(c[len("data: "):]) for c in content_chunks]
    assert parsed[0]["choices"][0]["delta"].get("role") == "assistant"
    assert any(
        p["choices"][0]["delta"].get("content") == "hello from fathom"
        for p in parsed
    )
    assert parsed[-1]["choices"][0]["finish_reason"] == "stop"
    assert all(p.get("session_id") == "test-session-slug" for p in parsed)


async def test_concurrent_sessions_get_their_own_replies(_patched, client):
    """Two sessions firing in parallel must each receive their own
    reply, not get cross-pollinated. The to:openai:<sid> address tag is
    the load-bearing scope."""
    from api import db

    # Mint two distinct sessions.
    sessions = {"s-alpha": "alpha-reply", "s-beta": "beta-reply"}
    _patched["_state"]["replies"] = sessions

    async def _get_session(sid):
        return {"id": sid, "title": "test"}

    # Override the auto-create behavior so explicit session_ids are honored.
    async def _create_session(title="New session"):
        raise AssertionError("create_session should not be called when session_id is supplied")

    # (Already monkeypatched in the fixture; just shadow create.)
    import api.db as _db_mod
    _db_mod.create_session = _create_session
    _db_mod.get_session = _get_session

    async def _send(sid):
        return await client.post(
            "/v1/chat/completions",
            json={
                "session_id": sid,
                "messages": [{"role": "user", "content": f"hi {sid}"}],
            },
        )

    r_alpha, r_beta = await asyncio.gather(_send("s-alpha"), _send("s-beta"))
    body_alpha = r_alpha.json()
    body_beta = r_beta.json()
    assert body_alpha["session_id"] == "s-alpha"
    assert body_beta["session_id"] == "s-beta"
    assert body_alpha["choices"][0]["message"]["content"] == "alpha-reply"
    assert body_beta["choices"][0]["message"]["content"] == "beta-reply"
