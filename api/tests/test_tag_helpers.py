"""Tests for api/_tags.py — shared tag-parsing helpers.

These replace ~9 inline `for t in tags: if t.startswith(...)` loops
scattered across chat_listener, db, reserved_tags, contacts, routines,
tools, and routes/agents. A subtle bug here would skew contact
resolution, chat-session routing, heartbeat hostname display, and the
reserved-tag gate's anchor-contact logic.
"""

from __future__ import annotations

from api._tags import has_any_tag_with_prefix, tag_suffix

# ── tag_suffix ────────────────────────────────────────────────────────


def test_tag_suffix_returns_string_after_prefix() -> None:
    assert tag_suffix(["chat:s1", "contact:bob"], "contact:") == "bob"


def test_tag_suffix_returns_first_match_in_order() -> None:
    """Multiple contact tags shouldn't happen in practice, but pin the
    semantics so later callers who rely on ordering aren't surprised."""
    assert tag_suffix(["contact:alice", "contact:bob"], "contact:") == "alice"


def test_tag_suffix_returns_none_when_no_prefix_matches() -> None:
    assert tag_suffix(["chat:s1", "other"], "contact:") is None


def test_tag_suffix_returns_none_on_empty_input() -> None:
    assert tag_suffix([], "contact:") is None
    assert tag_suffix(None, "contact:") is None


def test_tag_suffix_ignores_non_string_junk() -> None:
    """Older deltas occasionally have non-string entries in tags.
    The helper must skip them rather than crash — matches the
    isinstance check callers used to inline."""
    assert tag_suffix([None, 42, "contact:bob"], "contact:") == "bob"  # type: ignore[list-item]


def test_tag_suffix_allows_empty_suffix() -> None:
    """A tag that's exactly the prefix (e.g. 'contact:') produces an
    empty-string suffix. Treat as present-but-empty — caller decides
    whether that's meaningful."""
    assert tag_suffix(["contact:"], "contact:") == ""


def test_tag_suffix_prefix_must_match_exactly() -> None:
    """'contact-deleted' must NOT match prefix 'contact:' — the colon
    is part of the contract."""
    assert tag_suffix(["contact-deleted"], "contact:") is None


# ── has_any_tag_with_prefix ───────────────────────────────────────────


def test_has_any_tag_with_prefix_true_when_one_matches() -> None:
    assert has_any_tag_with_prefix(["contact:bob", "chat:s1"], "chat:") is True


def test_has_any_tag_with_prefix_false_when_none_match() -> None:
    assert has_any_tag_with_prefix(["foo", "bar"], "chat:") is False


def test_has_any_tag_with_prefix_false_on_empty_input() -> None:
    assert has_any_tag_with_prefix([], "chat:") is False
    assert has_any_tag_with_prefix(None, "chat:") is False


def test_has_any_tag_with_prefix_ignores_non_string_junk() -> None:
    assert has_any_tag_with_prefix([None, 1, "chat:s"], "chat:") is True  # type: ignore[list-item]
    assert has_any_tag_with_prefix([None, 1], "chat:") is False  # type: ignore[list-item]
