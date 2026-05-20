"""Tests for `routine-id` propagation across the dispatch chain.

The chain that surfaces routine context to a follow-up fire:

  routine-fire intent (carries `routine-id:<id>`)
    → harness fire reads pending, sets FIRE_ROUTINE_ID contextvar
    → dispatch_helper stamps `routine-id:<id>` onto the proposal
    → approve flow forwards it onto the kind:helper-dispatch delta
    → watcher reads the dispatch tags into the origin record
    → watcher mints the helper-reply intent with `routine-id:<id>`
    → next harness fire reads pending, sees the tag, loads the spec
    → ORIGINATING ROUTINE block lands in the system message

These tests pin the load-bearing tag flow at each link.
"""

from __future__ import annotations

from api.loop.claude_code_watcher import _build_intent_tags


def test_intent_tags_include_routine_id_when_origin_has_it():
    """When the watcher's origin lookup found a `routine-id:` on the
    dispatch delta, the helper-reply intent it mints carries it
    forward so the next fire's standpoint renders the routine spec."""
    info = {
        "host": "myras-fedora-laptop",
        "role": "claude-code",
        "project": "/home/myra",
        "origin": {"routine_id": "weekly-blog-post-reflection"},
    }
    tags = _build_intent_tags(
        corr="abc123",
        sid="session-xyz",
        info=info,
        source_id="delta-1",
        closure=False,
    )
    assert "routine-id:weekly-blog-post-reflection" in tags


def test_intent_tags_omit_routine_id_when_origin_lookup_empty():
    """Free-floating chat dispatches (no originating routine) must
    NOT acquire a stray routine-id — that would mislead the next
    fire's standpoint into loading an unrelated spec."""
    info = {
        "host": "myras-fedora-laptop",
        "role": "claude-code",
        "origin": {"channel": "chat", "correlation": "abc"},
    }
    tags = _build_intent_tags(
        corr="abc123",
        sid="session-xyz",
        info=info,
        source_id="delta-1",
        closure=False,
    )
    assert not any(t.startswith("routine-id:") for t in tags)


def test_intent_tags_carry_routine_id_on_closure_too():
    """Closure intents (the final task-complete) must also carry
    `routine-id:` — the closure is often the only fire that gets a
    substantive helper reply, so it's the most important link."""
    info = {
        "host": "myras-fedora-laptop",
        "role": "claude-code",
        "contact": "myra",
        "origin": {
            "routine_id": "weekly-blog-post-reflection",
            "channel": "chat",
        },
    }
    tags = _build_intent_tags(
        corr="abc123",
        sid="session-xyz",
        info=info,
        source_id="delta-1",
        closure=True,
    )
    assert "routine-id:weekly-blog-post-reflection" in tags
    assert "closure:true" in tags
