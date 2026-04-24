"""Unit test for the LRU eviction on ChatListener._session_locks.

Before the bound, a long-running process accumulated one asyncio.Lock
per session slug ever seen — tiny but unbounded. The LRU cap keeps the
working set of active sessions while evicting stale ones.
"""

from __future__ import annotations

from api.chat_listener import _SESSION_LOCK_CAP, ChatListener


def test_session_locks_cap_at_configured_size() -> None:
    listener = ChatListener()
    for i in range(_SESSION_LOCK_CAP + 50):
        listener._lock_for_session(f"s{i}")
    assert len(listener._session_locks) == _SESSION_LOCK_CAP
    # The 50 oldest slugs should have been evicted, the newest kept.
    assert "s0" not in listener._session_locks
    assert f"s{_SESSION_LOCK_CAP + 49}" in listener._session_locks


def test_session_locks_lru_reorders_on_hit() -> None:
    listener = ChatListener()
    for i in range(_SESSION_LOCK_CAP):
        listener._lock_for_session(f"s{i}")

    # Re-touching the oldest (s0) must move it to the end, so adding a
    # new slug evicts s1 instead of s0.
    listener._lock_for_session("s0")
    listener._lock_for_session("fresh")

    assert "s0" in listener._session_locks
    assert "s1" not in listener._session_locks
    assert "fresh" in listener._session_locks


def test_session_locks_repeat_access_returns_same_lock() -> None:
    """Two consecutive ticks for the same session must serialize through
    the same Lock — otherwise overlapping inference bursts race."""
    listener = ChatListener()
    first = listener._lock_for_session("s")
    second = listener._lock_for_session("s")
    assert first is second
