"""Tests for the image_path allowlist — the single defence against
arbitrary-file-read via POST /v1/deltas.

Threat model: any caller with a `lake:write` token can POST to
/v1/deltas. Before this guard, they could pass
`image_path="/etc/passwd"` (or `/data/tokens.json`, or any file the api
process can read) and the api would read it off disk, upload it into
the lake, and return a delta id. These tests lock in the containment
check that stops that cold.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from api.routes.lake import _resolve_allowed_image_path
from api.settings import settings


def test_image_path_disabled_by_default(monkeypatch) -> None:
    """Empty prefix (default) means the feature is off — every call fails."""
    monkeypatch.setattr(settings, "image_path_allowed_prefix", "")
    with pytest.raises(HTTPException) as exc:
        _resolve_allowed_image_path("/tmp/anything.jpg")
    assert exc.value.status_code == 400
    assert "disabled" in exc.value.detail


def test_image_path_inside_allowed_prefix_resolves(tmp_path, monkeypatch) -> None:
    """Happy path: a path under the configured prefix resolves fine."""
    monkeypatch.setattr(settings, "image_path_allowed_prefix", str(tmp_path))
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"fake-jpeg")

    resolved = _resolve_allowed_image_path(str(target))
    assert resolved == target.resolve()


def test_image_path_rejects_parent_traversal(tmp_path, monkeypatch) -> None:
    """The textbook attack: ../.. to escape the sandbox."""
    monkeypatch.setattr(settings, "image_path_allowed_prefix", str(tmp_path))
    # Build a path that starts inside tmp_path but resolves outside.
    escape = str(tmp_path / ".." / ".." / "etc" / "passwd")
    with pytest.raises(HTTPException) as exc:
        _resolve_allowed_image_path(escape)
    assert exc.value.status_code == 400


def test_image_path_rejects_absolute_outside_prefix(tmp_path, monkeypatch) -> None:
    """A caller supplying an absolute path outside the prefix gets rejected
    without needing traversal characters."""
    monkeypatch.setattr(settings, "image_path_allowed_prefix", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        _resolve_allowed_image_path("/etc/passwd")
    assert exc.value.status_code == 400


def test_image_path_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    """Symlinks inside the allowed prefix must not be able to point out.

    If a caller (or, more realistically, someone with filesystem access)
    drops a symlink inside the staging dir pointing at /etc/passwd, the
    resolved path leaves the prefix — still rejected.
    """
    monkeypatch.setattr(settings, "image_path_allowed_prefix", str(tmp_path))
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    try:
        link = tmp_path / "link.txt"
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this filesystem")

    with pytest.raises(HTTPException) as exc:
        _resolve_allowed_image_path(str(link))
    assert exc.value.status_code == 400


def test_image_path_accepts_deep_subpath(tmp_path, monkeypatch) -> None:
    """Nested directories inside the prefix still resolve."""
    monkeypatch.setattr(settings, "image_path_allowed_prefix", str(tmp_path))
    nested = tmp_path / "a" / "b" / "c.jpg"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")
    resolved = _resolve_allowed_image_path(str(nested))
    assert resolved == nested.resolve()
