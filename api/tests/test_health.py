"""Smoke test for /health. Public endpoint, no auth, no lake hits.

If this test fails on a clean checkout, something about app import or settings
defaults has regressed — start debugging there before anywhere else.
"""

from __future__ import annotations

import httpx


async def test_health_returns_ok(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # Without LLM_API_KEY set, health reports llm_configured=False and lists
    # what's missing. This asserts the reporting shape, not the config state.
    assert "llm_configured" in body
    assert "llm_missing" in body
    assert isinstance(body["llm_missing"], list)
