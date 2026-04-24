"""Regression tests for 404s on missing-resource paths.

Before this, four endpoints used `return {"error": "..."}, 404` which
FastAPI doesn't honour as a status-coded response — it serializes the
tuple as a 2-element JSON array `[{"error":"..."},404]` with HTTP 200.
Any client that branches on `r.status_code == 404` would never hit
that branch, and parsing the array as the expected resource shape
would crash or silently confuse the caller.

These tests lock the fix in place: every "missing resource" path
returns HTTP 404 with a proper detail body.
"""
from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from api import db, delta_client


@pytest.fixture
def _patch_client(monkeypatch):
    delta_client._client = None
    monkeypatch.setattr(delta_client, "_RETRY_BASE_DELAY", 0.0)


async def test_get_unknown_session_returns_404(monkeypatch) -> None:
    """If db.get_session returns None, the endpoint must surface HTTP 404
    (not `[{"error":"..."}, 404]` with HTTP 200 — the old tuple-return
    bug)."""
    from api.server import app

    async def _missing(_sid):
        return None

    monkeypatch.setattr(db, "get_session", _missing)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/v1/sessions/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert "detail" in body
    assert "not found" in body["detail"].lower()


async def test_patch_unknown_session_returns_404(monkeypatch) -> None:
    from api.server import app

    async def _missing(_sid, _title):
        return None

    monkeypatch.setattr(db, "update_session", _missing)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.patch("/v1/sessions/does-not-exist", json={"title": "nope"})
    assert r.status_code == 404


async def test_media_unknown_hash_returns_404(httpx_mock: HTTPXMock, _patch_client) -> None:
    from api.server import app

    # Delta-store returns 404 for unknown media; the proxy must translate
    # that to a proper 404 on its own endpoint (not a JSON tuple).
    httpx_mock.add_response(method="GET", status_code=404, is_reusable=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/v1/media/not-a-real-hash")
    assert r.status_code == 404
    body = r.json()
    assert "detail" in body
    assert "not found" in body["detail"].lower()


async def test_chat_completions_unknown_session_returns_404(monkeypatch) -> None:
    from api.server import app

    async def _missing(_sid):
        return None

    monkeypatch.setattr(db, "get_session", _missing)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post(
            "/v1/chat/completions",
            json={
                "session_id": "does-not-exist",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r.status_code == 404
    body = r.json()
    assert "detail" in body
    assert "not found" in body["detail"].lower()
