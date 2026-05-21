"""Tests for `api/loop/llm_gate` — per-tier LLM-down detection.

Pins:
  * Comparison of newest error vs newest heartbeat decides is_down
  * Heartbeat-only (no error) → up
  * Error-only (no heartbeat) → down
  * Newer heartbeat → up regardless of older errors
  * tier_status() returns parsed JSON payload when down
  * Probe budget is one-per-interval per key
  * Cache invalidation forces a re-read
  * Unknown tier → up (safe default)
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from api.loop import llm_gate


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts with empty cache + probe budget."""
    llm_gate.invalidate_cache()
    llm_gate.reset_probe_budget()
    yield
    llm_gate.invalidate_cache()
    llm_gate.reset_probe_budget()


def _delta(ts: str, content: str = "") -> dict:
    return {"timestamp": ts, "content": content}


@pytest.mark.asyncio
async def test_no_signals_means_up():
    with patch.object(llm_gate, "_newest_with_tags", AsyncMock(return_value=None)):
        assert await llm_gate.is_down("hard") is False
        assert await llm_gate.is_down("medium") is False


@pytest.mark.asyncio
async def test_heartbeat_only_means_up():
    async def fake_query(tags):
        if "kind:llm-heartbeat" in tags:
            return _delta("2026-05-21T10:00:00Z")
        return None
    with patch.object(llm_gate, "_newest_with_tags", side_effect=fake_query):
        assert await llm_gate.is_down("medium") is False


@pytest.mark.asyncio
async def test_error_only_means_down():
    async def fake_query(tags):
        if "system-error" in tags:
            return _delta("2026-05-21T10:00:00Z")
        return None
    with patch.object(llm_gate, "_newest_with_tags", side_effect=fake_query):
        assert await llm_gate.is_down("medium") is True


@pytest.mark.asyncio
async def test_newer_heartbeat_clears_old_error():
    async def fake_query(tags):
        if "system-error" in tags:
            return _delta("2026-05-21T09:00:00Z")
        if "kind:llm-heartbeat" in tags:
            return _delta("2026-05-21T10:00:00Z")
        return None
    with patch.object(llm_gate, "_newest_with_tags", side_effect=fake_query):
        assert await llm_gate.is_down("hard") is False


@pytest.mark.asyncio
async def test_newer_error_keeps_tier_down():
    async def fake_query(tags):
        if "system-error" in tags:
            return _delta("2026-05-21T11:00:00Z")
        if "kind:llm-heartbeat" in tags:
            return _delta("2026-05-21T10:00:00Z")
        return None
    with patch.object(llm_gate, "_newest_with_tags", side_effect=fake_query):
        assert await llm_gate.is_down("hard") is True


@pytest.mark.asyncio
async def test_tiers_are_independent():
    """Hard down + medium up are reported correctly per tier."""
    async def fake_query(tags):
        is_err = "system-error" in tags
        is_heartbeat = "kind:llm-heartbeat" in tags
        tier_tag = next((t for t in tags if t.startswith("system-error-tier:") or t.startswith("llm-tier:")), "")
        tier = tier_tag.split(":")[-1]
        if is_err and tier == "hard":
            return _delta("2026-05-21T11:00:00Z")
        if is_heartbeat and tier == "medium":
            return _delta("2026-05-21T11:00:00Z")
        return None
    with patch.object(llm_gate, "_newest_with_tags", side_effect=fake_query):
        assert await llm_gate.is_down("hard") is True
        assert await llm_gate.is_down("medium") is False


@pytest.mark.asyncio
async def test_unknown_tier_returns_up():
    """Defensive — never block on a tier we don't know."""
    assert await llm_gate.is_down("light") is False
    assert await llm_gate.is_down("") is False


@pytest.mark.asyncio
async def test_tier_status_returns_parsed_error_payload_when_down():
    payload = {"role": "Standard tasks", "class": "RateLimitError", "message": "credits depleted"}
    async def fake_query(tags):
        if "system-error" in tags:
            return _delta("2026-05-21T11:00:00Z", content=json.dumps(payload))
        return None
    with patch.object(llm_gate, "_newest_with_tags", side_effect=fake_query):
        st = await llm_gate.tier_status("medium")
    assert st["ok"] is False
    assert st["error"] == payload
    assert st["last_error_ts"] == "2026-05-21T11:00:00Z"
    assert st["last_heartbeat_ts"] is None


@pytest.mark.asyncio
async def test_tier_status_returns_no_error_when_up():
    async def fake_query(tags):
        if "kind:llm-heartbeat" in tags:
            return _delta("2026-05-21T11:00:00Z")
        return None
    with patch.object(llm_gate, "_newest_with_tags", side_effect=fake_query):
        st = await llm_gate.tier_status("hard")
    assert st["ok"] is True
    assert st["error"] is None
    assert st["last_heartbeat_ts"] == "2026-05-21T11:00:00Z"


@pytest.mark.asyncio
async def test_tier_status_handles_non_json_error_content():
    """Older error deltas might carry bare strings — don't crash."""
    async def fake_query(tags):
        if "system-error" in tags:
            return _delta("2026-05-21T11:00:00Z", content="raw text error not JSON")
        return None
    with patch.object(llm_gate, "_newest_with_tags", side_effect=fake_query):
        st = await llm_gate.tier_status("hard")
    assert st["ok"] is False
    assert st["error"] == {"message": "raw text error not JSON"}


def test_claim_probe_grants_one_per_interval():
    """First call wins, immediate second loses, after interval first wins again."""
    assert llm_gate.claim_probe("supervisor:hard", interval_s=0.05) is True
    assert llm_gate.claim_probe("supervisor:hard", interval_s=0.05) is False
    time.sleep(0.06)
    assert llm_gate.claim_probe("supervisor:hard", interval_s=0.05) is True


def test_claim_probe_keys_are_independent():
    assert llm_gate.claim_probe("a", interval_s=10) is True
    assert llm_gate.claim_probe("b", interval_s=10) is True  # different key
    assert llm_gate.claim_probe("a", interval_s=10) is False  # same key, rate-limited


@pytest.mark.asyncio
async def test_cache_avoids_repeated_lake_reads():
    call_count = {"err": 0, "hb": 0}
    async def fake_query(tags):
        if "system-error" in tags:
            call_count["err"] += 1
        if "kind:llm-heartbeat" in tags:
            call_count["hb"] += 1
        return None
    with patch.object(llm_gate, "_newest_with_tags", side_effect=fake_query):
        await llm_gate.is_down("hard")
        await llm_gate.is_down("hard")
        await llm_gate.is_down("hard")
    # All three calls within cache TTL → only one round-trip each.
    assert call_count == {"err": 1, "hb": 1}


@pytest.mark.asyncio
async def test_invalidate_cache_forces_reread():
    call_count = {"err": 0}
    async def fake_query(tags):
        if "system-error" in tags:
            call_count["err"] += 1
        return None
    with patch.object(llm_gate, "_newest_with_tags", side_effect=fake_query):
        await llm_gate.is_down("hard")
        llm_gate.invalidate_cache("hard")
        await llm_gate.is_down("hard")
    assert call_count["err"] == 2
