"""Tests for the helper inbox endpoint family.

Covers:
  · helper-scope token issuance (admin-gated, host-bound).
  · require_helper_host: a token bound to host A can't act on host B
    even with the right scope.
  · Inbox GET projects lake deltas to the slim helper shape and applies
    the optional role filter.
  · Inbox POST writes lake deltas with controlled tags only — extra
    tags pass an allowlist; unknown tag prefixes are dropped silently.

The lake (delta_client) is stubbed; the auth path runs against a fresh
on-disk tokens file per test (via the fixture in test_auth.py-style
isolation, copied here so this file is self-contained).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api import auth


@pytest.fixture(autouse=True)
def _isolate_tokens_file(tmp_path, monkeypatch):
    token_file = tmp_path / "tokens.json"
    monkeypatch.setattr(auth.settings, "tokens_path", str(token_file))
    auth._CONTACT_CACHE.clear()
    yield


# ── create_token + helper scope ───────────────────────────────────


def test_create_token_with_helper_scope_requires_host_binding() -> None:
    with pytest.raises(ValueError):
        auth.create_token(
            name="bad-helper",
            scopes=["helper"],
            contact_slug="myra",
            helper_host="",
        )


def test_create_token_helper_scope_persists_host_binding() -> None:
    result = auth.create_token(
        name="kitty@fedora",
        scopes=["helper"],
        contact_slug="myra",
        helper_host="fedora-laptop",
    )
    assert result["scopes"] == ["helper"]
    assert result["helper_host"] == "fedora-laptop"
    # validate() round-trip surfaces the binding for the middleware.
    record = auth.validate(result["token"])
    assert record is not None
    assert record["helper_host"] == "fedora-laptop"


# ── require_helper_host ───────────────────────────────────────────


def _request_with_token(token_record: dict | None) -> MagicMock:
    """Mock a Request with `state.token` set to the given record."""
    request = MagicMock()
    request.state.token = token_record
    return request


def test_require_helper_host_passes_when_path_matches_binding() -> None:
    token = {"helper_host": "fedora-laptop", "scopes": ["helper"]}
    out = auth.require_helper_host("fedora-laptop", _request_with_token(token))
    assert out == "fedora-laptop"


def test_require_helper_host_403_on_host_mismatch() -> None:
    """Token bound to fedora-laptop must NOT satisfy a request for nixos."""
    from fastapi import HTTPException

    token = {"helper_host": "fedora-laptop", "scopes": ["helper"]}
    with pytest.raises(HTTPException) as excinfo:
        auth.require_helper_host("nixos", _request_with_token(token))
    assert excinfo.value.status_code == 403


def test_require_helper_host_403_when_token_has_no_binding() -> None:
    """A non-helper token (or a malformed helper token) must be refused."""
    from fastapi import HTTPException

    token = {"helper_host": "", "scopes": ["lake:read", "lake:write"]}
    with pytest.raises(HTTPException) as excinfo:
        auth.require_helper_host("fedora-laptop", _request_with_token(token))
    assert excinfo.value.status_code == 403


def test_require_helper_host_401_when_no_token_on_request() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        auth.require_helper_host("fedora-laptop", _request_with_token(None))
    assert excinfo.value.status_code == 401


# ── _required_scope routing for helper paths ──────────────────────


def test_helper_inbox_path_requires_helper_scope() -> None:
    """GET on the inbox is gated by the `helper` scope (host check is
    a separate gate, applied in the route handler)."""
    assert auth._required_scope("GET", "/v1/helpers/fedora-laptop/inbox") == "helper"
    assert (
        auth._required_scope("POST", "/v1/helpers/fedora-laptop/inbox/abc123/reply")
        == "helper"
    )


def test_admin_helper_token_paths_require_tokens_manage() -> None:
    """Minting/listing/revoking helper tokens is admin-only — `helper`
    scope on its own MUST NOT mint more `helper` tokens."""
    assert (
        auth._required_scope("POST", "/v1/admin/helpers/fedora-laptop/tokens")
        == "tokens:manage"
    )
    assert (
        auth._required_scope("GET", "/v1/admin/helpers/fedora-laptop/tokens")
        == "tokens:manage"
    )
    assert (
        auth._required_scope("DELETE", "/v1/admin/helpers/fedora-laptop/tokens/abc12345")
        == "tokens:manage"
    )


# ── inbox slim projection + reply tag handling ────────────────────


def test_slim_dispatch_pulls_role_corr_and_task_body_from_witness_payload() -> None:
    from api.routes import helpers as h

    delta = {
        "id": "lake-delta-id-1",
        "timestamp": "2026-05-08T17:00:00Z",
        "tags": [
            "feed-card",
            "route:helper",
            "route:helper:claude-code",
            "host:fedora-laptop",
            "to:helper:abc123def456",
            "task-corr:abc123def456",
            "helper-role:claude-code",
        ],
        "content": (
            '{"kicker":"helper · claude-code @ fedora-laptop","title":"do a thing",'
            '"body":"investigate the foo and report back","route":"helper:claude-code"}'
        ),
    }
    slim = h._slim_dispatch(delta)
    assert slim is not None
    assert slim["role"] == "claude-code"
    assert slim["corr"] == "abc123def456"
    assert slim["task"] == "investigate the foo and report back"
    assert slim["delta_id"] == "lake-delta-id-1"
    assert slim["kind"] == "dispatch"


def test_slim_dispatch_skips_when_role_or_corr_missing() -> None:
    """A delta with the umbrella `route:helper` tag but no role-specific
    tag and no corr is not actionable — return None instead of surfacing
    a half-formed inbox row."""
    from api.routes import helpers as h

    delta = {
        "id": "x",
        "timestamp": "2026-05-08T17:00:00Z",
        "tags": ["route:helper", "host:fedora-laptop"],
        "content": "no body",
    }
    assert h._slim_dispatch(delta) is None


def test_slim_dispatch_takes_plain_content_when_payload_isnt_json() -> None:
    """The proposal-approve path (api/routes/proposals.py) writes the
    task as plain content. Helper inbox should surface that as `task`
    without trying to JSON-decode."""
    from api.routes import helpers as h

    delta = {
        "id": "approved-1",
        "timestamp": "2026-05-08T17:00:00Z",
        "tags": [
            "route:helper:claude-code",
            "host:fedora-laptop",
            "to:helper:cafef00d",
            "task-corr:cafef00d",
            "helper-role:claude-code",
        ],
        "content": "do the thing",
    }
    slim = h._slim_dispatch(delta)
    assert slim is not None
    assert slim["task"] == "do the thing"


def test_filter_extra_tags_keeps_allowlisted_prefixes() -> None:
    from api.routes import helpers as h

    extras = [
        "claude-code-session:sess-abc",
        "project:/home/myra/Dropbox/Work",
        "task-spawn",
        # Should be dropped — not in allowlist
        "kind:proposal",
        "affirms:lake-delta-1",
        "engages:abc",
        "  ",
    ]
    out = h._filter_extra_tags(extras)
    assert out == [
        "claude-code-session:sess-abc",
        "project:/home/myra/Dropbox/Work",
        "task-spawn",
    ]


def test_filter_extra_tags_dedupes() -> None:
    from api.routes import helpers as h

    out = h._filter_extra_tags(["task-spawn", "task-spawn", "project:/x"])
    assert out == ["task-spawn", "project:/x"]
