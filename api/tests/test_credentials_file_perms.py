"""File-permission tests for the two on-disk credential stores.

Both tokens.json and pair-codes.json contain password-equivalent
material — SHA-256 token hashes that are password-equivalent for the
auth gate, and pair codes that are single-use admission tickets. A
default-umask write (0644) would make them world-readable on the host,
turning a multi-user box into a shared-secrets problem.

Skipped on non-POSIX platforms because chmod there is a no-op.
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

from api import auth, pairing


@pytest.mark.skipif(sys.platform.startswith("win"), reason="chmod semantics are POSIX-specific")
def test_tokens_file_mode_is_0600(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "tokens.json"
    monkeypatch.setattr(auth.settings, "tokens_path", str(token_file))
    auth.create_token(name="perm-test")
    mode = stat.S_IMODE(os.stat(token_file).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="chmod semantics are POSIX-specific")
def test_pair_codes_file_mode_is_0600(tmp_path, monkeypatch) -> None:
    codes_file = tmp_path / "pair-codes.json"
    monkeypatch.setattr(pairing.settings, "pair_codes_path", str(codes_file))
    pairing.create_pair_code(ttl_seconds=60, contact_slug="bob")
    mode = stat.S_IMODE(os.stat(codes_file).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
