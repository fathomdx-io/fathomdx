"""The MCP/CLI-facing provenance endpoint and its tool registration.

`POST /v1/provenance` lets an episodic agent (a Claude Code session
using Fathom as memory over MCP) crystallize provenance on an event
without the Grand Loop running. It must be advertised through
/v1/tools scoped to lake:write, genuinely scope-gated at the route,
and dispatch to the same shared create path the harness uses.
"""

from __future__ import annotations

import types

import pytest
from fastapi import HTTPException

from api import auth, provenance_centroid
from api import provenance_create as pc
from api.loop import puddle as puddle_mod
from api.routes import lake as lake_routes


def _req(token_name: str | None = "Pim"):
    token = {"name": token_name} if token_name is not None else None
    return types.SimpleNamespace(state=types.SimpleNamespace(token=token))


def _wire_lake(monkeypatch: pytest.MonkeyPatch, *, resolve: bool = True) -> dict:
    recorded: dict = {"writes": []}

    async def fake_get_delta(fid):
        if not resolve:
            return None
        return {"id": fid, "tags": ["kind:thread-msg"], "embedding": [0.1] * 512}

    async def fake_write(**kwargs):
        recorded["writes"].append(kwargs)
        return {"id": f"delta-{len(recorded['writes'])}"}

    async def fake_centroid(from_ids):
        return [0.5] * provenance_centroid.VECTOR_DIM

    async def fake_puddle_write(**kwargs):
        return {"id": "puddle"}

    monkeypatch.setattr(pc.delta_client, "get_delta", fake_get_delta)
    monkeypatch.setattr(pc.delta_client, "write", fake_write)
    monkeypatch.setattr(provenance_centroid, "compute_centroid", fake_centroid)
    monkeypatch.setattr(puddle_mod.puddle, "write", fake_puddle_write)
    return recorded


# ── Tool registration & scope wiring ───────────────────────────────────


def test_tool_is_registered_for_mcp_lake_write():
    tool = next((t for t in lake_routes.LAKE_TOOLS if t["name"] == "propose_provenance"), None)
    assert tool is not None, "propose_provenance missing from LAKE_TOOLS"
    assert tool["scope"] == "lake:write"
    assert "mcp" in tool["surfaces"]
    assert tool["endpoint"] == {"method": "POST", "path": "/v1/provenance"}
    assert tool["response_kind"] == "result_text"
    assert set(tool["parameters"]["required"]) == {"title", "summary", "from_ids"}


def test_endpoint_requires_lake_write_scope():
    """The route is genuinely scope-gated by the auth middleware, not just
    hidden from the tool list."""
    assert auth._required_scope("POST", "/v1/provenance") == "lake:write"


def test_producer_derived_from_token_name():
    assert lake_routes._agent_producer(_req("Pim")) == "episodic-agent-pim"
    assert lake_routes._agent_producer(_req("Unnamed token")) == "episodic-agent"
    assert lake_routes._agent_producer(_req(None)) == "episodic-agent"


# ── Endpoint behavior ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_from_ids_returns_400(monkeypatch: pytest.MonkeyPatch):
    _wire_lake(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await lake_routes.create_provenance_endpoint(
            _req(), {"title": "t", "summary": "s", "from_ids": []}
        )
    assert ei.value.status_code == 400
    assert "at least" in ei.value.detail


@pytest.mark.asyncio
async def test_hallucinated_ids_return_400(monkeypatch: pytest.MonkeyPatch):
    _wire_lake(monkeypatch, resolve=False)
    with pytest.raises(HTTPException) as ei:
        await lake_routes.create_provenance_endpoint(
            _req(),
            {"title": "t", "summary": "s", "from_ids": ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]},
        )
    assert ei.value.status_code == 400
    assert "do not resolve" in ei.value.detail


@pytest.mark.asyncio
async def test_valid_request_crystallizes(monkeypatch: pytest.MonkeyPatch):
    recorded = _wire_lake(monkeypatch)
    out = await lake_routes.create_provenance_endpoint(
        _req("Pim"),
        {
            "title": "Trapped, two ways free",
            "summary": "Wojtek and Houdini — two escapes from a box.",
            "level": 1,
            "from_ids": ["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
            "rationale": "both escape confinement",
        },
    )
    assert "result" in out
    assert "Crystallized L1 provenance" in out["result"]
    assert "2 constituents" in out["result"]
    # The proposal card is stamped with the agent's derived producer.
    assert "produced-by:episodic-agent-pim" in recorded["writes"][0]["tags"]
