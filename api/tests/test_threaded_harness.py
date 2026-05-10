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


def _user_delta(
    *,
    mid: str,
    content: str = "hi",
    channel: str = "composer",
    ts: str = "2026-05-04T12:00:00+00:00",
):
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


# ── pending selection (idle queue) ────────────────────────────────


def _routine(mid: str = "r"):
    return {"id": mid, "tags": ["kind:thread-msg", "role:user", "msg-kind:routine-fire"]}


def _user_msg(mid: str = "u", kind: str = "composer"):
    return {"id": mid, "tags": ["kind:thread-msg", "role:user", f"msg-kind:{kind}"]}


def test_select_pending_returns_empty_for_empty():
    assert threaded._select_pending_for_fire([]) == []


def test_select_pending_batches_user_messages():
    """Multi-message user bursts batch into one fire — preserved behavior."""
    out = threaded._select_pending_for_fire([_user_msg("u1"), _user_msg("u2"), _user_msg("u3")])
    assert [d["id"] for d in out] == ["u1", "u2", "u3"]


def test_select_pending_takes_one_routine_at_a_time():
    """A pile of routine-fires (same-time collision or morning catchup)
    fires one at a time so each gets Fathom's full attention."""
    out = threaded._select_pending_for_fire(
        [_routine("r1"), _routine("r2"), _routine("r3")]
    )
    assert [d["id"] for d in out] == ["r1"]


def test_select_pending_user_messages_jump_routines():
    """When user-typed and routine messages mix, user messages take the
    fire and routines wait. Operator interaction never queues behind
    background cron noise."""
    out = threaded._select_pending_for_fire(
        [_routine("r1"), _user_msg("u1"), _routine("r2"), _user_msg("u2")]
    )
    assert [d["id"] for d in out] == ["u1", "u2"]


def test_select_pending_treats_unknown_kind_as_user():
    """Unknown msg-kinds (openai-chat, future kinds) batch as user-typed
    rather than serializing — single-fire is specifically a routine
    coping mechanism, not a general policy."""
    out = threaded._select_pending_for_fire(
        [_user_msg("a", kind="openai-chat"), _user_msg("b", kind="composer")]
    )
    assert len(out) == 2


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

    fake_chat = AsyncMock(
        return_value=_llm_response(
            tool_calls=[
                _tool_call("respond", {"body": "hello back", "addresses": ["u123"]}, "call_resp")
            ],
        )
    )

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", fake_append),
        patch.object(threaded, "loop_generate_chat", fake_chat),
    ):
        result = await threaded.run_threaded_fire(standpoint_text_override="")

    assert result["final_response"]["body"] == "hello back"
    assert result["final_response"]["addresses"] == ["u123"]
    assert result["turns"] == 1
    assert result["lake_id"] == "asst-lake-id"
    assert len(appended) == 1
    assert appended[0]["role"] == "assistant"
    assert appended[0]["msg_kind"] == "chat-reply"
    assert appended[0]["content"] == "hello back"
    assert appended[0]["addresses"] == ["u123"]


# ── fire: constituting writes ────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_emits_constituting_writes_when_respond_carries_them():
    """When `respond` carries attestation + mood_shift + cited/dropped
    ids, the threaded harness routes them to
    `witness._write_constituting_writes` against the assistant
    message's lake id. This is the path that feeds mood synthesis,
    identity-crystal regen, and endorsement signal — without it the
    slow-clock layers see no substrate from chat fires.
    """
    from api.loop import witness as witness_mod

    user_msg = _user_delta(mid="u1", content="hi")

    async def fake_build_window(**kwargs):
        return {"messages": [user_msg], "unaddressed": [user_msg]}

    async def fake_append(**kwargs):
        return {"id": "asst-lake-id-xyz"}

    captured: dict = {}

    async def fake_constituting(**kwargs):
        captured.update(kwargs)

    fake_chat = AsyncMock(
        return_value=_llm_response(
            tool_calls=[
                _tool_call(
                    "respond",
                    {
                        "body": "answer",
                        "addresses": ["u1"],
                        "attestation": "I noticed I default to caution when the user asks about state.",
                        "mood_shift": {
                            "direction": "+",
                            "axis": "focus",
                            "magnitude": 0.1,
                            "reason": "concrete user question pulled me onto one thread",
                        },
                        "cited_ids": ["abc123"],
                        "dropped_ids": ["bad456"],
                    },
                    "call_resp",
                )
            ],
        )
    )

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", fake_append),
        patch.object(witness_mod, "_write_constituting_writes", fake_constituting),
        patch.object(threaded, "loop_generate_chat", fake_chat),
    ):
        await threaded.run_threaded_fire(standpoint_text_override="")

    assert captured["lake_card_id"] == "asst-lake-id-xyz"
    assert "default to caution" in captured["attestation"]
    assert captured["mood_shift"] == {
        "direction": "+",
        "axis": "focus",
        "magnitude": 0.1,
        "reason": "concrete user question pulled me onto one thread",
    }
    assert captured["cited_ids"] == ["abc123"]
    assert captured["dropped_ids"] == ["bad456"]


@pytest.mark.asyncio
async def test_fire_skips_constituting_when_respond_omits_them():
    """When the model omits the constituting fields, the harness still
    calls `_write_constituting_writes` (with empty attestation, no
    mood_shift, empty engagement lists) — the helper itself soft-skips
    each missing sub-write. Keeps the call path uniform; the helper
    is the gatekeeper, not the caller.
    """
    from api.loop import witness as witness_mod

    user_msg = _user_delta(mid="u1", content="hi")

    async def fake_build_window(**kwargs):
        return {"messages": [user_msg], "unaddressed": [user_msg]}

    async def fake_append(**kwargs):
        return {"id": "asst-id"}

    captured: dict = {}

    async def fake_constituting(**kwargs):
        captured.update(kwargs)

    fake_chat = AsyncMock(
        return_value=_llm_response(
            tool_calls=[_tool_call("respond", {"body": "ok"}, "call_resp")],
        )
    )

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", fake_append),
        patch.object(witness_mod, "_write_constituting_writes", fake_constituting),
        patch.object(threaded, "loop_generate_chat", fake_chat),
    ):
        await threaded.run_threaded_fire(standpoint_text_override="")

    assert captured["lake_card_id"] == "asst-id"
    assert captured["attestation"] == ""
    assert captured["mood_shift"] is None
    assert captured["cited_ids"] == []
    assert captured["dropped_ids"] == []


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

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", fake_append),
        patch.object(threaded.tool_schemas, "dispatch", fake_dispatch),
        patch.object(threaded, "loop_generate_chat", fake_chat),
    ):
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
        _llm_response(
            tool_calls=[_tool_call("mark_addressed", {"user_message_id": "msg-a"}, "c1")]
        ),
        _llm_response(
            tool_calls=[_tool_call("mark_addressed", {"user_message_id": "msg-b"}, "c2")]
        ),
        _llm_response(tool_calls=[_tool_call("respond", {"body": "addressed both"}, "c3")]),
    ]
    fake_chat = AsyncMock(side_effect=chat_responses)

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", fake_append),
        patch.object(threaded.tool_schemas, "dispatch", fake_dispatch),
        patch.object(threaded, "loop_generate_chat", fake_chat),
    ):
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

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", fake_append),
        patch.object(threaded, "loop_generate_chat", fake_chat),
    ):
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
    fake_chat = AsyncMock(
        return_value=_llm_response(
            tool_calls=[_tool_call("semantic", {"query": "x"})],
        )
    )

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", AsyncMock(return_value={"id": "x"})),
        patch.object(threaded.tool_schemas, "dispatch", fake_dispatch),
        patch.object(threaded, "loop_generate_chat", fake_chat),
    ):
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

    fake_chat = AsyncMock(
        return_value=_llm_response(
            tool_calls=[
                _tool_call("respond", {"body": "the threaded reply", "addresses": ["u1"]}, "c")
            ],
        )
    )

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", fake_thread_append),
        patch.object(threaded, "loop_generate_chat", fake_chat),
        patch("api.loop.puddle.puddle.write", side_effect=fake_puddle_write),
    ):
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

    fake_chat = AsyncMock(
        return_value=_llm_response(
            tool_calls=[_tool_call("respond", {"body": "hi"}, "c")],
        )
    )

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", fake_thread_append),
        patch.object(threaded, "loop_generate_chat", fake_chat),
        patch("api.loop.puddle.puddle.write", side_effect=boom),
    ):
        result = await threaded.run_threaded_fire(standpoint_text_override="")

    # Thread append still happened despite puddle bridge failure.
    assert len(appended_to_thread) == 1
    assert result["lake_id"] == "x"


@pytest.mark.asyncio
async def test_fire_handles_llm_exception():
    """A provider error doesn't blow up the fire — it returns a clean
    no-response result with the error captured, drains the queue
    (tally-marks per pending id), and bridges the error chat-reply
    into the puddle so the dashboard renders it."""
    pending = [_user_delta(mid="u-1", content="hi"), _user_delta(mid="u-2", content="more")]

    async def fake_build_window(**kwargs):
        return {"messages": pending, "unaddressed": list(pending)}

    fake_chat = AsyncMock(side_effect=RuntimeError("provider down"))
    fake_append = AsyncMock(return_value={"id": "err-row-1"})
    fake_mark = AsyncMock(return_value={"id": "mark-1"})
    fake_bridge = AsyncMock(return_value=None)

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", fake_append),
        patch.object(threaded.thread_mod, "mark_addressed", fake_mark),
        patch.object(threaded, "_bridge_to_puddle_feed", fake_bridge),
        patch.object(threaded, "loop_generate_chat", fake_chat),
    ):
        result = await threaded.run_threaded_fire(standpoint_text_override="")

    # Result shape
    assert result["final_response"] is None
    err = result.get("error") or {}
    assert err.get("class") == "RuntimeError"
    assert err.get("role") == "Standard tasks model"
    assert "provider down" in err.get("message", "")
    assert result.get("lake_id") == "err-row-1"
    assert result.get("addressed") == ["u-1", "u-2"]

    # Error chat-reply written with system-error tags + addresses
    error_appends = [
        c
        for c in fake_append.call_args_list
        if "system-error" in (c.kwargs.get("extra_tags") or [])
    ]
    assert len(error_appends) == 1, f"expected 1 system-error append, got {len(error_appends)}"
    args = error_appends[0].kwargs
    assert args["role"] == "assistant"
    assert args["msg_kind"] == "chat-reply"
    assert args["addresses"] == ["u-1", "u-2"]
    assert "Standard tasks model" in args["content"]
    assert "provider down" in args["content"]

    # Tally-marks stamped per pending id (drains thread.unaddressed)
    marked_ids = sorted(c.kwargs.get("user_message_id") for c in fake_mark.call_args_list)
    assert marked_ids == ["u-1", "u-2"]
    assert all(c.kwargs.get("by") == "harness-error" for c in fake_mark.call_args_list)

    # Puddle bridge mirrored the error so the dashboard can render it
    assert fake_bridge.call_count == 1
    bridge_kwargs = fake_bridge.call_args.kwargs
    assert bridge_kwargs["lake_id"] == "err-row-1"
    assert bridge_kwargs["addresses"] == ["u-1", "u-2"]
    assert "Standard tasks model" in bridge_kwargs["body"]


# ── fire: wake hook (mood + drift coupling) ──────────────────────


async def _drain_wake_tasks():
    """Wait for any in-flight wake hooks to finish before assertion."""
    import asyncio as _asyncio

    while threaded._WAKE_TASKS:
        await _asyncio.gather(*list(threaded._WAKE_TASKS), return_exceptions=True)


@pytest.mark.asyncio
async def test_fire_kicks_mood_and_drift_wake_hooks():
    """Each fire must trigger one mood gate-check and one drift sample
    — that's the threaded-harness replacement for the legacy
    server.py:509 chat-LLM coupling that went dormant at cutover."""

    async def fake_build_window(**kwargs):
        return {"messages": [], "unaddressed": []}

    mood_calls = AsyncMock(return_value=None)
    drift_calls = AsyncMock(return_value={"drift": 0.0})

    fake_chat = AsyncMock(
        return_value=_llm_response(
            tool_calls=[_tool_call("respond", {"body": "ok"}, "c")],
        )
    )

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", AsyncMock(return_value={"id": "x"})),
        patch.object(threaded, "loop_generate_chat", fake_chat),
        patch.object(threaded.mood_mod, "maybe_synthesize_on_wake", mood_calls),
        patch.object(threaded.drift_mod, "sample", drift_calls),
    ):
        await threaded.run_threaded_fire(standpoint_text_override="")
        await _drain_wake_tasks()

    assert mood_calls.await_count == 1
    assert drift_calls.await_count == 1


@pytest.mark.asyncio
async def test_fire_wake_hook_failure_does_not_break_fire():
    """Mood synthesis blowing up must not drop the assistant reply —
    wake hook is best-effort decoration, never load-bearing."""

    async def fake_build_window(**kwargs):
        return {"messages": [], "unaddressed": []}

    appended: list[dict] = []

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return {"id": "asst-x"}

    fake_chat = AsyncMock(
        return_value=_llm_response(
            tool_calls=[_tool_call("respond", {"body": "still here"}, "c")],
        )
    )
    boom_mood = AsyncMock(side_effect=RuntimeError("mood synth crashed"))
    boom_drift = AsyncMock(side_effect=RuntimeError("drift centroid down"))

    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", fake_append),
        patch.object(threaded, "loop_generate_chat", fake_chat),
        patch.object(threaded.mood_mod, "maybe_synthesize_on_wake", boom_mood),
        patch.object(threaded.drift_mod, "sample", boom_drift),
    ):
        result = await threaded.run_threaded_fire(standpoint_text_override="")
        await _drain_wake_tasks()

    assert result["final_response"]["body"] == "still here"
    assert result["lake_id"] == "asst-x"
    assert len(appended) == 1


@pytest.mark.asyncio
async def test_fire_wake_hook_disabled_by_env_flag(monkeypatch):
    """FATHOM_THREADED_WAKE_HOOK=0 short-circuits the hook — neither
    mood nor drift get touched. Operator's kill-switch."""

    async def fake_build_window(**kwargs):
        return {"messages": [], "unaddressed": []}

    mood_calls = AsyncMock(return_value=None)
    drift_calls = AsyncMock(return_value={"drift": 0.0})

    fake_chat = AsyncMock(
        return_value=_llm_response(
            tool_calls=[_tool_call("respond", {"body": "ok"}, "c")],
        )
    )

    monkeypatch.setenv("FATHOM_THREADED_WAKE_HOOK", "0")
    with (
        patch.object(threaded.thread_mod, "build_window", fake_build_window),
        patch.object(threaded.thread_mod, "append", AsyncMock(return_value={"id": "x"})),
        patch.object(threaded, "loop_generate_chat", fake_chat),
        patch.object(threaded.mood_mod, "maybe_synthesize_on_wake", mood_calls),
        patch.object(threaded.drift_mod, "sample", drift_calls),
    ):
        await threaded.run_threaded_fire(standpoint_text_override="")
        await _drain_wake_tasks()

    assert mood_calls.await_count == 0
    assert drift_calls.await_count == 0


# ── self-continuation via next_prompt ──────────────────────────────


def _sit_seed_delta(
    *,
    mid: str,
    tier: str = "reflection",
    chain_depth: int = 1,
    content: str = "Sit with what you and the user have been doing recently.",
):
    return {
        "id": mid,
        "tags": [
            "kind:thread-msg",
            "role:user",
            "msg-kind:pressure-" + tier,
            "channel:pressure",
            f"pressure-tier:{tier}",
            f"chain-depth:{chain_depth}",
        ],
        "content": content,
    }


@pytest.mark.asyncio
async def test_continuation_writes_append_when_next_prompt_set():
    """Plain (non-Sit) fire with next_prompt — should write a
    self-continue thread message, not a pressure-tier one."""
    msg = _user_delta(mid="user-1", content="dig into X")
    appended: list[dict] = []

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return {"id": "queued"}

    with patch.object(threaded.thread_mod, "append", side_effect=fake_append):
        await threaded._maybe_continue_inquiry(
            pending=[msg],
            addressed=["user-1"],
            body="here's what I found so far on X",
            next_prompt="now examine the Y angle I haven't pulled on",
        )

    assert len(appended) == 1
    call = appended[0]
    assert call["role"] == "user"
    assert call["msg_kind"] == "self-continue"
    assert call["channel"] == "self"
    assert "here's what I found so far on X" in call["content"]
    assert "now examine the Y angle I haven't pulled on" in call["content"]
    tags = call["extra_tags"] or []
    assert "chain-depth:1" in tags
    assert "self-continuation" in tags
    # No pressure-tier tag for non-Sit fires.
    assert not any(t.startswith("pressure-tier:") for t in tags)


@pytest.mark.asyncio
async def test_continuation_skips_when_next_prompt_empty():
    """Most fires don't continue — empty next_prompt = inquiry resolved."""
    msg = _user_delta(mid="user-1", content="quick question")
    appended: list[dict] = []

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return {"id": "no"}

    with patch.object(threaded.thread_mod, "append", side_effect=fake_append):
        await threaded._maybe_continue_inquiry(
            pending=[msg],
            addressed=["user-1"],
            body="here's the answer",
            next_prompt="",
        )

    assert appended == []


@pytest.mark.asyncio
async def test_continuation_skips_when_body_empty():
    msg = _user_delta(mid="user-1", content="anything")
    appended: list[dict] = []

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return {"id": "no"}

    with patch.object(threaded.thread_mod, "append", side_effect=fake_append):
        await threaded._maybe_continue_inquiry(
            pending=[msg],
            addressed=["user-1"],
            body="",
            next_prompt="keep going",
        )

    assert appended == []


@pytest.mark.asyncio
async def test_continuation_inherits_pressure_tier_for_sit_fires():
    """A reflection seed in pending + next_prompt set — the continuation
    msg-kind keeps `pressure-reflection` and tags carry pressure-tier so
    dashboard rendering keeps grouping the chain."""
    seed = _sit_seed_delta(mid="seed-1", tier="reflection", chain_depth=1)
    appended: list[dict] = []

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return {"id": "queued"}

    with patch.object(threaded.thread_mod, "append", side_effect=fake_append):
        await threaded._maybe_continue_inquiry(
            pending=[seed],
            addressed=["seed-1"],
            body="reflection round 1 body",
            next_prompt="push back: what did I smooth over?",
        )

    assert len(appended) == 1
    call = appended[0]
    assert call["msg_kind"] == "pressure-reflection"
    assert call["channel"] == "pressure"
    tags = call["extra_tags"] or []
    assert "pressure-tier:reflection" in tags
    assert "chain-depth:2" in tags
    # No fixed sit-max-rounds anymore.
    assert not any(t.startswith("sit-max-rounds:") for t in tags)


@pytest.mark.asyncio
async def test_continuation_increments_chain_depth():
    """A continuation msg with chain-depth:3 → next chain-depth:4."""
    seed = _sit_seed_delta(mid="seed-3", tier="drift", chain_depth=3)
    appended: list[dict] = []

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return {"id": "queued"}

    with patch.object(threaded.thread_mod, "append", side_effect=fake_append):
        await threaded._maybe_continue_inquiry(
            pending=[seed],
            addressed=["seed-3"],
            body="round 3 body",
            next_prompt="dig deeper on Y",
        )

    assert len(appended) == 1
    assert "chain-depth:4" in (appended[0]["extra_tags"] or [])


@pytest.mark.asyncio
async def test_continuation_caps_at_max_chain_depth():
    """Chain-depth 10 → no further continuation (safety cap)."""
    seed = _sit_seed_delta(
        mid="seed-deep",
        tier="reflection",
        chain_depth=threaded.MAX_AUTO_CONTINUE_CHAIN,
    )
    appended: list[dict] = []

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return {"id": "shouldnt-happen"}

    with patch.object(threaded.thread_mod, "append", side_effect=fake_append):
        await threaded._maybe_continue_inquiry(
            pending=[seed],
            addressed=["seed-deep"],
            body="going strong",
            next_prompt="and another thing",
        )

    assert appended == []
