"""Tests for `/v1/llm/status` — the per-tier banner endpoint.

Pins the response shape because the dashboard JS at
`dashboard/index.html` reads top-level `ok` / `error` to drive the
banner; new per-tier `hard` / `medium` keys are additive.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.routes import vitals


async def _fake_tier_status_factory(*, hard_ok: bool, medium_ok: bool,
                                    hard_err_ts: str = "",
                                    medium_err_ts: str = "",
                                    hard_payload: dict | None = None,
                                    medium_payload: dict | None = None):
    """Return a mock conforming to llm_gate.tier_status()'s contract."""

    async def fake(tier: str):
        if tier == "hard":
            return {
                "ok": hard_ok,
                "error": hard_payload if not hard_ok else None,
                "last_heartbeat_ts": None,
                "last_error_ts": hard_err_ts or None,
            }
        return {
            "ok": medium_ok,
            "error": medium_payload if not medium_ok else None,
            "last_heartbeat_ts": None,
            "last_error_ts": medium_err_ts or None,
        }

    return fake


@pytest.mark.asyncio
async def test_both_tiers_ok_returns_top_level_ok():
    fake = await _fake_tier_status_factory(hard_ok=True, medium_ok=True)
    with patch.object(vitals.llm_gate, "tier_status", side_effect=fake):
        r = await vitals.get_llm_status()
    assert r["ok"] is True
    assert r["error"] is None
    assert r["hard"]["ok"] is True
    assert r["medium"]["ok"] is True


@pytest.mark.asyncio
async def test_medium_down_only_surfaces_in_top_level():
    fake = await _fake_tier_status_factory(
        hard_ok=True,
        medium_ok=False,
        medium_err_ts="2026-05-21T11:00:00Z",
        medium_payload={"role": "Standard tasks", "class": "RateLimitError", "message": "credits depleted"},
    )
    with patch.object(vitals.llm_gate, "tier_status", side_effect=fake):
        r = await vitals.get_llm_status()
    assert r["ok"] is False
    assert r["error"]["message"] == "credits depleted"
    assert r["error"]["role"] == "Standard tasks"
    assert r["error"]["timestamp"] == "2026-05-21T11:00:00Z"
    assert r["hard"]["ok"] is True
    assert r["medium"]["ok"] is False


@pytest.mark.asyncio
async def test_both_down_top_level_picks_most_recent():
    """Two tiers down at different timestamps — banner should reflect the
    newer one so the user sees the freshest failure."""
    fake = await _fake_tier_status_factory(
        hard_ok=False,
        medium_ok=False,
        hard_err_ts="2026-05-21T10:00:00Z",
        hard_payload={"role": "Main", "class": "Timeout", "message": "hard timed out"},
        medium_err_ts="2026-05-21T11:00:00Z",
        medium_payload={"role": "Standard tasks", "class": "RateLimitError", "message": "medium rate-limited"},
    )
    with patch.object(vitals.llm_gate, "tier_status", side_effect=fake):
        r = await vitals.get_llm_status()
    assert r["ok"] is False
    # Medium is the newer error → it surfaces in the top-level shape.
    assert r["error"]["message"] == "medium rate-limited"
    assert r["error"]["timestamp"] == "2026-05-21T11:00:00Z"
    assert r["hard"]["ok"] is False
    assert r["medium"]["ok"] is False


@pytest.mark.asyncio
async def test_missing_error_fields_default_safely():
    """An error payload with empty fields should not crash — endpoint
    fills defaults so the dashboard always has strings to render."""
    fake = await _fake_tier_status_factory(
        hard_ok=False,
        medium_ok=True,
        hard_err_ts="2026-05-21T10:00:00Z",
        hard_payload={},  # totally empty
    )
    with patch.object(vitals.llm_gate, "tier_status", side_effect=fake):
        r = await vitals.get_llm_status()
    assert r["error"]["role"] == "LLM"
    assert r["error"]["class"] == ""
    assert r["error"]["message"] == ""
