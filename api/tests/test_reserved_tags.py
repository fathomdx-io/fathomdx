"""Reserved-tag gate tests — the authority boundary for /v1/deltas.

This module is the single check between external callers and lake writes
that carry authority-bearing tags. If any of these gates silently
degrade to "allow" the security model of the API evaporates. Tests
cover every gate branch plus the strip-and-re-stamp helper that keeps
callers from addressing deltas to someone else.
"""
from __future__ import annotations

import pytest

from api.reserved_tags import (
    GATE_ADMIN_OR_SELF,
    GATE_INTERNAL,
    GATE_SESSION_MEMBER_OR_ADMIN,
    GateResult,
    evaluate,
    hint_for,
    resolve,
    strip_contact_tags,
)

# ── strip_contact_tags ────────────────────────────────────────────────

def test_strip_contact_tags_drops_all_contact_tags() -> None:
    tags = ["chat:s", "contact:bob", "user", "contact:mallory"]
    assert strip_contact_tags(tags) == ["chat:s", "user"]


def test_strip_contact_tags_preserves_non_contact_tags() -> None:
    tags = ["foo", "bar:baz", "quux"]
    assert strip_contact_tags(tags) == tags


def test_strip_contact_tags_handles_empty_input() -> None:
    assert strip_contact_tags([]) == []
    assert strip_contact_tags(None) == []  # type: ignore[arg-type]


def test_strip_contact_tags_ignores_non_string_entries() -> None:
    """Defensive: tags list occasionally contains junk (pydantic coerces,
    but older deltas have oddities). Don't crash, just skip."""
    tags = ["chat:s", None, 42, "contact:bob"]  # type: ignore[list-item]
    assert strip_contact_tags(tags) == ["chat:s", None, 42]  # type: ignore[list-item]


# ── resolve ───────────────────────────────────────────────────────────

def test_resolve_returns_none_for_plain_data_tags() -> None:
    assert resolve("chat:abc") is None
    assert resolve("feed-engagement") is None
    assert resolve("contact:bob") is None
    assert resolve("random-user-tag") is None


def test_resolve_returns_internal_gate_for_crystal_identity() -> None:
    assert resolve("crystal:identity") == GATE_INTERNAL


def test_resolve_returns_admin_or_self_for_agent_heartbeat() -> None:
    assert resolve("agent-heartbeat") == GATE_ADMIN_OR_SELF


def test_resolve_matches_prefix_for_handle_bindings() -> None:
    """Prefix matching: any 'handle:*' is internal-gated."""
    assert resolve("handle:email:bob@example.com") == GATE_INTERNAL


def test_resolve_is_robust_to_non_string_input() -> None:
    assert resolve(None) is None  # type: ignore[arg-type]
    assert resolve(42) is None  # type: ignore[arg-type]


# ── hint_for ──────────────────────────────────────────────────────────

def test_hint_for_returns_endpoint_pointer() -> None:
    hint = hint_for("crystal:identity")
    assert "/v1/crystal/refresh" in hint


def test_hint_for_unknown_tag_returns_empty_string() -> None:
    assert hint_for("random-tag") == ""


# ── evaluate ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluate_passes_plain_data_tags() -> None:
    result = await evaluate(["chat:s", "user"], {"slug": "bob", "role": "member"})
    assert result == GateResult(True)


@pytest.mark.asyncio
async def test_evaluate_rejects_internal_tag_even_for_admin() -> None:
    """GATE_INTERNAL means 'use the named endpoint' — admins don't get
    to bypass by dropping it on /v1/deltas."""
    result = await evaluate(
        ["crystal:identity"], {"slug": "admin", "role": "admin"}
    )
    assert result.ok is False
    assert result.tag == "crystal:identity"
    assert result.gate == GATE_INTERNAL


@pytest.mark.asyncio
async def test_evaluate_admin_or_self_passes_for_admin() -> None:
    result = await evaluate(
        ["agent-heartbeat", "contact:bob"], {"slug": "admin", "role": "admin"}
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_evaluate_admin_or_self_passes_for_own_contact() -> None:
    """After strip-and-re-stamp in the endpoint, the tag's contact is the
    caller's slug. Self-write of your own heartbeat is allowed."""
    result = await evaluate(
        ["agent-heartbeat", "contact:bob"], {"slug": "bob", "role": "member"}
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_evaluate_admin_or_self_rejects_foreign_contact() -> None:
    """If the tag's contact:X doesn't match the caller, reject.
    This shouldn't happen post-strip-and-re-stamp, but belt-and-suspenders."""
    result = await evaluate(
        ["agent-heartbeat", "contact:alice"], {"slug": "bob", "role": "member"}
    )
    assert result.ok is False
    assert result.gate == GATE_ADMIN_OR_SELF


@pytest.mark.asyncio
async def test_evaluate_rejects_agent_heartbeat_without_any_caller() -> None:
    """Unauthenticated writers cannot produce authority-bearing tags."""
    result = await evaluate(["agent-heartbeat"], None)
    assert result.ok is False


@pytest.mark.asyncio
async def test_evaluate_rejects_handle_prefix_tag() -> None:
    """handle:* is internal — admin endpoint owns uniqueness."""
    result = await evaluate(
        ["handle:email:bob@example.com", "contact:bob"],
        {"slug": "bob", "role": "admin"},
    )
    assert result.ok is False
    assert result.gate == GATE_INTERNAL


@pytest.mark.asyncio
async def test_evaluate_session_member_admin_always_passes(monkeypatch) -> None:
    """Admin bypasses the session-member check."""
    # Patch is_session_member to fail loudly if it's consulted — admin
    # path should not consult it.
    async def _boom(*args, **kwargs):
        raise AssertionError("is_session_member should not be called for admins")

    monkeypatch.setattr("api.reserved_tags.is_session_member", _boom)

    # There are no GATE_SESSION_MEMBER_OR_ADMIN tags in the current
    # registry, so this test exercises the "admin bypasses" branch via
    # a synthetic injection.
    from api import reserved_tags

    monkeypatch.setitem(
        reserved_tags._EXACT, "synthetic-gate-tag", GATE_SESSION_MEMBER_OR_ADMIN
    )

    result = await evaluate(
        ["synthetic-gate-tag", "chat:s"], {"slug": "admin", "role": "admin"}
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_evaluate_rejects_unknown_gate_fail_closed(monkeypatch) -> None:
    """If the registry ever names an unknown gate string, reject — never
    default to allow."""
    from api import reserved_tags

    monkeypatch.setitem(reserved_tags._EXACT, "mystery-tag", "UNKNOWN_GATE")

    result = await evaluate(["mystery-tag"], {"slug": "admin", "role": "admin"})
    assert result.ok is False
    assert result.gate == "UNKNOWN_GATE"
