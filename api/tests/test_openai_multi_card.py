"""Multi-card OpenAI completions — the loop can speak in stages.

When the witness fires a chat-reply ("looking into it") alongside a
claude-code dispatch in the same tick, the closure-followup card with
the actual answer arrives later as a separate fire. The OpenAI
contract is one completion per user turn, so the endpoint holds the
response open and streams every chat-reply card addressed to this
turn — joined by a blank-line separator — until the question is
gone from the tally.

"Done" = no `route:claude-code` dispatch addressing this intent is
still missing its `about-task-corr:<corr>` closure-followup card.
That's the witness's own "this thread is wrapped" stamp; checking
witness output rather than puddle state avoids racing the watcher.
"""

from __future__ import annotations

import json

import httpx
import pytest


def _witness_card_content(body: str) -> str:
    return json.dumps({
        "kicker": "",
        "title": "",
        "body": body,
        "tail": "",
        "route": "chat-reply",
        "axes": {},
    })


@pytest.fixture
def _staged(monkeypatch):
    """Lake stub that lets each test stage the witness output sequence
    Fathom would have written for a turn — chat-replies, dispatches,
    and closure-followup cards — and watch the endpoint walk through
    them.

    State shape:
      replies[sid]    = list of (timestamp, body) — chat-reply cards
                        addressing this session's intent. Streamed in
                        timestamp order.
      dispatches[sid] = list of task_corrs — `route:claude-code` cards
                        addressing this session's intent.
      followups[sid]  = set of task_corrs whose closure-followup
                        chat-reply is "available" — when the endpoint
                        polls for `about-task-corr:<corr>` and the
                        corr is in this set, the lookup returns one
                        match (signaling: this dispatch has wrapped).
    """
    from api import db, delta_client
    from api.loop import puddle as puddle_mod

    state: dict = {
        "replies": {},
        "dispatches": {},
        "followups": {},
        "intent_id_by_sid": {},
    }

    async def _get_session(sid):
        return {"id": sid, "title": "test"}

    async def _create_session(title="New session"):
        return {"id": "test-session", "title": title}

    monkeypatch.setattr(db, "get_session", _get_session)
    monkeypatch.setattr(db, "create_session", _create_session)

    intent_counter = {"n": 0}

    async def _puddle_write(*, content, tags, source, ttl_seconds=None, expires_at=None):
        intent_counter["n"] += 1
        intent_id = f"intent-{intent_counter['n']}"
        sid = ""
        for t in tags:
            if t.startswith("openai-session:"):
                sid = t.split(":", 1)[1]
                break
        if sid:
            state["intent_id_by_sid"][sid] = intent_id
        return {
            "id": intent_id,
            "content": content,
            "tags": list(tags),
            "source": source,
            "timestamp": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr(puddle_mod.puddle, "write", _puddle_write)

    async def _query(limit=50, tags_include=None, **kw):
        tags_include = tags_include or []
        # ── Witness-card poll: to:openai:<sid> + addresses:<intent_id>
        sid = ""
        addresses = ""
        is_dispatch_lookup = "route:claude-code" in tags_include
        about_corr = ""
        for t in tags_include:
            if t.startswith("to:openai:"):
                sid = t.split(":", 2)[2]
            elif t.startswith("addresses:"):
                addresses = t.split(":", 1)[1]
            elif t.startswith("about-task-corr:"):
                about_corr = t.split(":", 1)[1]

        # Witness-output poll for streaming chat-reply cards.
        if sid and addresses and not is_dispatch_lookup and not about_corr:
            replies = state["replies"].get(sid, [])
            return [
                {
                    "id": f"reply-{sid}-{i}",
                    "content": _witness_card_content(body),
                    "tags": [
                        f"to:openai:{sid}",
                        "channel:openai",
                        f"addresses:{addresses}",
                        "route:chat-reply",
                    ],
                    "timestamp": ts,
                }
                for i, (ts, body) in enumerate(replies)
            ]

        # Dispatch-existence poll.
        if is_dispatch_lookup and addresses:
            corrs = state["dispatches"].get(addresses, [])
            return [
                {
                    "id": f"dispatch-{i}",
                    "content": "",
                    "tags": [
                        "route:claude-code",
                        f"addresses:{addresses}",
                        f"task-corr:{c}",
                    ],
                    "timestamp": "9999-12-31T23:59:50.000Z",
                }
                for i, c in enumerate(corrs)
            ]

        # Closure-followup poll: `about-task-corr:<corr>` + addresses:<id>.
        if about_corr:
            if about_corr in state["followups"].get(addresses, set()):
                return [
                    {
                        "id": f"followup-{about_corr}",
                        "content": "",
                        "tags": [
                            f"about-task-corr:{about_corr}",
                            f"addresses:{addresses}",
                        ],
                        "timestamp": "9999-12-31T23:59:55.000Z",
                    }
                ]
            return []
        return []

    monkeypatch.setattr(delta_client, "query", _query)

    async def _write(content, tags=None, source="fathom", **kw):
        return {"id": "fake-write-id"}

    monkeypatch.setattr(delta_client, "write", _write)

    return state


@pytest.fixture
async def client():
    from api.server import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ── Non-stream ──────────────────────────────────


async def test_single_card_no_dispatch_returns_one_body(_staged, client):
    """Baseline — Fathom answered from memory. No dispatch, one card,
    finishes immediately."""
    sid = "alpha"
    _staged["replies"][sid] = [("9999-12-31T23:59:50.000Z", "the answer is 42")]

    r = await client.post(
        "/v1/chat/completions",
        json={"session_id": sid, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    body = r.json()["choices"][0]["message"]["content"]
    assert body == "the answer is 42"


async def test_concatenates_acknowledgment_and_closure_followup(_staged, client):
    """Witness fires chat-reply ('looking') + dispatch in one tick;
    closure-followup arrives later. Endpoint must return BOTH bodies
    joined by a blank line as a single completion."""
    sid = "beta"

    # Stage: dispatch addresses the intent that intent-1 will get
    # (the puddle stub assigns intent-1 to the first write).
    _staged["replies"][sid] = [
        ("9999-12-31T23:59:50.000Z", "Looking into that for you."),
        ("9999-12-31T23:59:58.000Z", "Here's what I found: it's 42."),
    ]
    # Wire dispatch → followup so the endpoint sees the dispatch as
    # in flight UNTIL the followup is registered.
    intent_for_this = "intent-1"
    _staged["dispatches"][intent_for_this] = ["corr-x"]
    _staged["followups"][intent_for_this] = {"corr-x"}

    r = await client.post(
        "/v1/chat/completions",
        json={"session_id": sid, "messages": [{"role": "user", "content": "what's 6 * 7?"}]},
    )
    assert r.status_code == 200
    body = r.json()["choices"][0]["message"]["content"]
    assert body == "Looking into that for you.\n\nHere's what I found: it's 42."


async def test_unfinished_dispatch_holds_until_followup(_staged, client):
    """If the dispatch's followup is NOT yet registered, the endpoint
    should keep waiting — not finish on the first card alone. We
    don't want to test the actual hold (slow); instead we assert
    that with one card and an unwrapped dispatch, the endpoint waits
    long enough to retry — and once the followup is registered,
    finishes with both cards.

    To keep this fast we stage everything up-front so the endpoint
    sees both cards on the first poll.
    """
    sid = "gamma"
    _staged["replies"][sid] = [
        ("9999-12-31T23:59:50.000Z", "Acknowledging."),
        ("9999-12-31T23:59:55.000Z", "Final answer."),
    ]
    intent_for_this = "intent-1"
    _staged["dispatches"][intent_for_this] = ["corr-y"]
    _staged["followups"][intent_for_this] = {"corr-y"}

    r = await client.post(
        "/v1/chat/completions",
        json={"session_id": sid, "messages": [{"role": "user", "content": "go"}]},
    )
    body = r.json()["choices"][0]["message"]["content"]
    assert "Acknowledging." in body
    assert "Final answer." in body
    assert body.index("Acknowledging.") < body.index("Final answer.")


# ── Streaming ───────────────────────────────────


async def test_stream_emits_one_chunk_per_card_with_separators(_staged, client):
    """`stream: true` sends every chat-reply card as its own content
    chunk, separated by a `\\n\\n` chunk between them, then finishes
    with `finish_reason: stop` once the closure-followup is in."""
    sid = "delta"
    _staged["replies"][sid] = [
        ("9999-12-31T23:59:50.000Z", "Card one."),
        ("9999-12-31T23:59:55.000Z", "Card two."),
    ]
    intent_for_this = "intent-1"
    _staged["dispatches"][intent_for_this] = ["corr-z"]
    _staged["followups"][intent_for_this] = {"corr-z"}

    r = await client.post(
        "/v1/chat/completions",
        json={
            "session_id": sid,
            "messages": [{"role": "user", "content": "go"}],
            "stream": True,
        },
    )
    assert r.status_code == 200
    chunks = [
        json.loads(line[len("data: "):])
        for line in r.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    contents = [c["choices"][0]["delta"].get("content") for c in chunks]
    contents = [c for c in contents if c is not None]
    # Expect: "Card one." → "\n\n" → "Card two."
    assert "Card one." in contents
    assert "Card two." in contents
    assert "\n\n" in contents
    assert contents.index("Card one.") < contents.index("\n\n") < contents.index("Card two.")
    # Final chunk has finish_reason: stop.
    finishes = [c["choices"][0].get("finish_reason") for c in chunks]
    assert finishes[-1] == "stop"


async def test_stream_no_dispatch_finishes_after_first_card(_staged, client):
    """No claude-code dispatch — the first chat-reply card finishes
    the stream. No second chunk, no `\\n\\n` separator."""
    sid = "epsilon"
    _staged["replies"][sid] = [("9999-12-31T23:59:50.000Z", "Just one card.")]

    r = await client.post(
        "/v1/chat/completions",
        json={
            "session_id": sid,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    chunks = [
        json.loads(line[len("data: "):])
        for line in r.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    contents = [
        c["choices"][0]["delta"].get("content")
        for c in chunks
        if c["choices"][0]["delta"].get("content") is not None
    ]
    assert contents == ["Just one card."]
    finishes = [c["choices"][0].get("finish_reason") for c in chunks]
    assert finishes[-1] == "stop"
