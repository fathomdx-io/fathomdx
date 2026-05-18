"""Verify the introspect tool is exposed via MCP/CLI surfaces.

Asserts the registry shape (LAKE_TOOLS entry, surface filter, endpoint),
the HTTP endpoint's input validation, and the agent-instructions text
that teaches external harnesses they can call Fathom this way.

Doesn't fire the harness — that path runs a real LLM round. The
end-to-end "claude-code calls introspect" smoke happens by hitting the
running api with a real token.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest


async def test_introspect_in_mcp_tools_list(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/tools?surface=mcp")
    assert r.status_code == 200
    names = [t["name"] for t in r.json().get("tools", [])]
    assert "introspect" in names


async def test_introspect_in_cli_tools_list(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/tools?surface=cli")
    assert r.status_code == 200
    names = [t["name"] for t in r.json().get("tools", [])]
    assert "introspect" in names


async def test_introspect_not_in_chat_surface(client: httpx.AsyncClient) -> None:
    # The chat surface goes through the threaded harness, which calls
    # introspect natively. Adding it to LAKE_TOOLS chat surface would
    # duplicate it; verify the registry keeps that boundary.
    r = await client.get("/v1/tools?surface=chat")
    assert r.status_code == 200
    names = [t["name"] for t in r.json().get("tools", [])]
    assert "introspect" not in names


async def test_introspect_endpoint_requires_question(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/introspect", json={})
    assert r.status_code == 400
    assert "question is required" in r.json().get("detail", "")


async def test_introspect_endpoint_rejects_oversize_question(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/introspect", json={"question": "x" * 1501})
    assert r.status_code == 400
    assert "too long" in r.json().get("detail", "")


@pytest.mark.asyncio
async def test_introspect_endpoint_calls_tool(client: httpx.AsyncClient) -> None:
    # Stub tool_introspect so we don't fire the harness. Just verify the
    # endpoint wires the question through and returns the body.
    with patch(
        "api.loop.harness.tools.tool_introspect",
        AsyncMock(return_value='introspect("hi") →\n\nfathom answer body'),
    ):
        r = await client.post("/v1/introspect", json={"question": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert "fathom answer body" in body.get("result", "")


def test_agent_instructions_teach_introspect() -> None:
    from api import agent_instructions

    for surface in ("helper", "mcp", "cli"):
        text = agent_instructions.get(surface)
        assert "introspect" in text, f"{surface} surface missing introspect docs"
