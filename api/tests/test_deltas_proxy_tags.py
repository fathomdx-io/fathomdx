"""Regression test for the /v1/deltas GET tags_include contract.

CLI and MCP both send `tags_include` as a comma-separated string
(e.g. `?tags_include=foo,bar`). Delta-store's /deltas expects a
repeated query param (list[str]) and matches each tag via postgres
`@>`. Before the fix, the proxy forwarded the raw string, delta-store
parsed it as a single-element list ["foo,bar"], and the @> filter
matched a delta tag literally named "foo,bar" — which never exists.
Result: every multi-tag recall returned empty.

If this test regresses, `fathom recall --tags a,b` silently stops
filtering again.
"""
from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from api import delta_client


@pytest.fixture
def _patch_client(monkeypatch):
    delta_client._client = None
    monkeypatch.setattr(delta_client, "_RETRY_BASE_DELAY", 0.0)


async def test_proxy_deltas_splits_csv_tags_into_repeated_param(
    httpx_mock: HTTPXMock, _patch_client
) -> None:
    """End-to-end: a GET to /v1/deltas?tags_include=foo,bar must reach
    delta-store as tags_include=foo&tags_include=bar (two params), not
    as a single tags_include=foo,bar."""
    from api.server import app

    # One mock per expected internal call. Middleware may make extra
    # lookups (e.g. first-admin-slug on first-run); pad generously.
    # is_reusable=True lets the same mock match multiple calls — enough
    # for any incidental middleware lookups in addition to the one we
    # care about.
    httpx_mock.add_response(method="GET", json=[], is_reusable=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/v1/deltas?tags_include=foo,bar")
    assert r.status_code == 200

    # Find the /deltas proxy call among any incidental middleware reads.
    requests = [
        req for req in httpx_mock.get_requests()
        if req.url.path == "/deltas"
    ]
    assert len(requests) >= 1
    sent_url = str(requests[-1].url)
    # Order doesn't matter, but both must be present as separate params.
    assert "tags_include=foo" in sent_url
    assert "tags_include=bar" in sent_url
    # And NOT as a single "foo,bar" concatenation — the original bug shape.
    assert "tags_include=foo%2Cbar" not in sent_url
    assert "tags_include=foo,bar" not in sent_url


async def test_proxy_deltas_single_tag_sent_as_list_of_one(
    httpx_mock: HTTPXMock, _patch_client
) -> None:
    """Single-tag recall still works — sent as one tags_include param."""
    from api.server import app

    # is_reusable=True lets the same mock match multiple calls — enough
    # for any incidental middleware lookups in addition to the one we
    # care about.
    httpx_mock.add_response(method="GET", json=[], is_reusable=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/v1/deltas?tags_include=chat:s")
    assert r.status_code == 200

    requests = [
        req for req in httpx_mock.get_requests()
        if req.url.path == "/deltas"
    ]
    sent_url = str(requests[-1].url)
    # URL-encoded colon is %3A; either form is fine.
    assert "tags_include=chat" in sent_url


async def test_proxy_deltas_empty_tag_filter_sends_no_tag_param(
    httpx_mock: HTTPXMock, _patch_client
) -> None:
    """Blank or whitespace-only tag string must not be forwarded at all —
    otherwise delta-store sees an empty-string tag and filters to nothing."""
    from api.server import app

    # is_reusable=True lets the same mock match multiple calls — enough
    # for any incidental middleware lookups in addition to the one we
    # care about.
    httpx_mock.add_response(method="GET", json=[], is_reusable=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/v1/deltas?tags_include=,,")
    assert r.status_code == 200

    requests = [
        req for req in httpx_mock.get_requests()
        if req.url.path == "/deltas"
    ]
    sent_url = str(requests[-1].url)
    assert "tags_include" not in sent_url
