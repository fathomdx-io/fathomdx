"""Tests for auth.py — token CRUD, scope gating, contact-slug helper.

The scope matrix and token validation are the load-bearing pieces here.
A broken `_required_scope` silently downgrades auth; a broken `validate`
can let a wrong token pass or reject a right one.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api import auth


@pytest.fixture(autouse=True)
def _isolate_tokens_file(tmp_path, monkeypatch):
    """Point every auth.* call at a fresh token store in tmp_path.

    Without this the tests would mutate /data/tokens.json on the dev box
    (or race other tests running in parallel). Each test gets an empty
    file and a clean contact cache.
    """
    token_file = tmp_path / "tokens.json"
    monkeypatch.setattr(auth.settings, "tokens_path", str(token_file))
    auth._CONTACT_CACHE.clear()
    yield


# ── _required_scope ──────────────────────────────────────────────────


def test_required_scope_maps_lake_read_endpoints() -> None:
    assert auth._required_scope("GET", "/v1/deltas") == "lake:read"
    assert auth._required_scope("GET", "/v1/tags") == "lake:read"
    assert auth._required_scope("GET", "/v1/stats") == "lake:read"


def test_required_scope_maps_writes_to_lake_write() -> None:
    assert auth._required_scope("POST", "/v1/deltas") == "lake:write"
    assert auth._required_scope("POST", "/v1/media/upload") == "lake:write"


def test_required_scope_maps_chat_session_routes() -> None:
    assert auth._required_scope("POST", "/v1/chat/completions") == "chat"


def test_required_scope_none_for_unmatched_route() -> None:
    assert auth._required_scope("GET", "/nonexistent") is None
    # Method mismatch: POST to a path that's only registered for GET.
    assert auth._required_scope("PUT", "/v1/tags") is None


def test_required_scope_matches_by_prefix_not_exact() -> None:
    """/v1/sources/abc/pause should match the /v1/sources prefix."""
    assert auth._required_scope("POST", "/v1/sources/abc/pause") == "sources:manage"


# ── create_token / validate / list / delete ─────────────────────────


def test_create_token_returns_raw_token_and_record() -> None:
    result = auth.create_token(name="agent-prod", scopes=["lake:read"])
    assert result["token"].startswith(auth.TOKEN_PREFIX)
    assert result["name"] == "agent-prod"
    assert result["scopes"] == ["lake:read"]
    # Raw hash never leaves the function — only the prefix teaser.
    assert "hash" not in result
    assert result["prefix"].endswith("…")


def test_create_token_defaults_to_all_scopes() -> None:
    result = auth.create_token()
    assert set(result["scopes"]) == set(auth.ALL_SCOPES.keys())


def test_create_token_filters_unknown_scope_strings() -> None:
    """Garbage scope values get dropped rather than stored and later
    matched as-if-granted — a subtle way to confuse the scope gate."""
    result = auth.create_token(scopes=["lake:read", "galaxy:conquer"])
    assert result["scopes"] == ["lake:read"]


def test_create_token_empty_scopes_falls_back_to_defaults() -> None:
    """Empty list after filtering means the caller didn't name any real
    scope. Better to grant defaults than mint a useless token."""
    result = auth.create_token(scopes=["nope"])
    assert set(result["scopes"]) == set(auth.ALL_SCOPES.keys())


def test_validate_accepts_created_token() -> None:
    raw = auth.create_token(contact_slug="bob")["token"]
    record = auth.validate(raw)
    assert record is not None
    assert record["contact_slug"] == "bob"


def test_validate_rejects_unknown_token() -> None:
    assert auth.validate("fth_totally-fake") is None


def test_validate_updates_last_used_at() -> None:
    raw = auth.create_token()["token"]
    tokens_before = auth._load()
    assert tokens_before[0]["last_used_at"] is None

    auth.validate(raw)

    tokens_after = auth._load()
    assert tokens_after[0]["last_used_at"] is not None


def test_list_tokens_strips_hashes() -> None:
    auth.create_token(name="a")
    auth.create_token(name="b")
    listed = auth.list_tokens()
    assert len(listed) == 2
    for t in listed:
        assert "hash" not in t


def test_delete_token_removes_by_id() -> None:
    token_id = auth.create_token()["id"]
    assert auth.delete_token(token_id) is True
    assert auth.list_tokens() == []


def test_delete_token_returns_false_for_unknown_id() -> None:
    assert auth.delete_token("nonexistent") is False


# ── migrate_legacy_tokens ───────────────────────────────────────────


def test_migrate_legacy_tokens_backfills_missing_contact_slug() -> None:
    """Legacy tokens minted before contact-awareness had no slug. The
    migration pins them to the first admin so every token has an owner."""
    # Synthesize a legacy record by hand (create_token always stamps slug).
    auth._save(
        [
            {
                "id": "legacy1",
                "name": "old",
                "hash": "x",
                "prefix": "fth_old…",
                "scopes": ["lake:read"],
                "created_at": "2024-01-01",
                "last_used_at": None,
                # contact_slug missing
            }
        ]
    )
    migrated = auth.migrate_legacy_tokens(default_slug="first-admin")
    assert migrated == 1
    assert auth._load()[0]["contact_slug"] == "first-admin"


def test_migrate_legacy_tokens_is_idempotent() -> None:
    """Running the migration twice must not keep re-stamping."""
    auth._save([{"id": "x", "hash": "y", "contact_slug": "bob"}])
    assert auth.migrate_legacy_tokens(default_slug="ignored") == 0


def test_migrate_legacy_tokens_noop_when_no_default_slug() -> None:
    """Pre-bootstrap: no admin exists, so no default to migrate to."""
    auth._save([{"id": "x", "hash": "y"}])  # no contact_slug
    assert auth.migrate_legacy_tokens(default_slug="") == 0
    # Token list untouched
    assert "contact_slug" not in auth._load()[0]


# ── current_contact_slug ───────────────────────────────────────────


def test_current_contact_slug_reads_request_state() -> None:
    req = MagicMock()
    req.state.contact = {"slug": "bob", "role": "member"}
    assert auth.current_contact_slug(req) == "bob"


def test_current_contact_slug_returns_empty_when_no_contact() -> None:
    req = MagicMock()
    req.state.contact = None
    assert auth.current_contact_slug(req) == ""


def test_current_contact_slug_returns_empty_when_state_missing() -> None:
    """Pre-auth-middleware or failed-auth request has no .state.contact
    at all. Must not crash — return empty string for the downstream
    filter-contact-scoped paths to degrade safely."""

    class _BareState:
        pass

    req = MagicMock()
    req.state = _BareState()
    # getattr(..., "contact", None) path — should return "" not raise.
    assert auth.current_contact_slug(req) == ""


# ── invalidate_contact_cache ───────────────────────────────────────


def test_invalidate_contact_cache_drops_one_slug() -> None:
    auth._CONTACT_CACHE["bob"] = (0.0, {"slug": "bob"})
    auth._CONTACT_CACHE["alice"] = (0.0, {"slug": "alice"})

    auth.invalidate_contact_cache("bob")

    assert "bob" not in auth._CONTACT_CACHE
    assert "alice" in auth._CONTACT_CACHE


def test_invalidate_contact_cache_drops_all_when_no_slug() -> None:
    auth._CONTACT_CACHE["bob"] = (0.0, {})
    auth._CONTACT_CACHE["alice"] = (0.0, {})

    auth.invalidate_contact_cache(None)

    assert auth._CONTACT_CACHE == {}
