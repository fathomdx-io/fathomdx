"""Unit tests for delta_client's retry-on-transient-failure helper.

These exercise the retry contract directly (without the consumer API
app) by swapping the module-level httpx client for a pytest-httpx mock.
If this test regresses, a real delta-store restart will start manifesting
as lost reads for the dashboard instead of a brief pause.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pytest_httpx import HTTPXMock

from api import delta_client


@pytest.fixture
def _patch_client(monkeypatch):
    """Force a fresh httpx.AsyncClient and zero-delay backoff.

    pytest-httpx mocks requests on the default httpx.AsyncClient, so we
    need to close any pre-existing module client and let delta_client
    build a new one. Backoff base is squashed to zero so tests don't
    sleep perceptible time — the actual _RETRY_BASE_DELAY value isn't
    the contract under test, the retry behaviour is.
    """
    delta_client._client = None  # next _get() builds a fresh mocked client
    monkeypatch.setattr(delta_client, "_RETRY_BASE_DELAY", 0.0)
    # Unused import kept for IDE hints on what `asyncio` means in context.
    _ = asyncio


async def test_retries_on_503_then_succeeds(httpx_mock: HTTPXMock, _patch_client):
    """Two 503s in a row should retry; the third (200) resolves normally."""
    httpx_mock.add_response(method="GET", status_code=503)
    httpx_mock.add_response(method="GET", status_code=503)
    httpx_mock.add_response(method="GET", json={"ok": True})

    r = await delta_client._request_with_retry("GET", "/stats")

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert len(httpx_mock.get_requests()) == 3


async def test_raises_after_exhausting_attempts(httpx_mock: HTTPXMock, _patch_client):
    """If every attempt gets a retryable error, the last error propagates."""
    httpx_mock.add_response(method="GET", status_code=502)
    httpx_mock.add_response(method="GET", status_code=502)
    httpx_mock.add_response(method="GET", status_code=502)

    with pytest.raises(httpx.HTTPStatusError):
        await delta_client._request_with_retry("GET", "/stats")

    assert len(httpx_mock.get_requests()) == delta_client._RETRY_ATTEMPTS


async def test_no_retry_on_4xx(httpx_mock: HTTPXMock, _patch_client):
    """4xx responses are NOT transient — return them without retrying."""
    httpx_mock.add_response(method="GET", status_code=404)

    r = await delta_client._request_with_retry("GET", "/deltas/missing")

    assert r.status_code == 404
    assert len(httpx_mock.get_requests()) == 1


async def test_retries_on_timeout(httpx_mock: HTTPXMock, _patch_client):
    """A single httpx.TimeoutException is retried; second attempt succeeds."""
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"))
    httpx_mock.add_response(method="GET", json={"ok": True})

    r = await delta_client._request_with_retry("GET", "/stats")

    assert r.status_code == 200
    assert len(httpx_mock.get_requests()) == 2
