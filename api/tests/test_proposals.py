"""Proposal-card endpoint coverage.

approve → reads card, calls tool handler with structured args, writes
decision delta. deny → just writes the decision. get → returns the
proposal + the latest decision (if any). 404 when the delta isn't a
proposal card.

Auth is bypassed by overriding `auth.require_admin` via FastAPI's
dependency_overrides — monkeypatching the module attribute doesn't
work because Depends() captures the function reference at import time.
"""

from __future__ import annotations

import json

import pytest


def _proposal_card(delta_id: str = "p-1", tool_args: dict | None = None) -> dict:
    args = tool_args or {
        "action": "create",
        "id": "ramen-check",
        "name": "Menya Rui hours check",
        "schedule": "10 10 * * *",
        "prompt": "check if Menya Rui is open",
        "host": "fedora",
    }
    payload = {
        "kicker": "Routine?",
        "title": "Daily ramen check",
        "body": "Set this up daily at 10:10?",
        "route": "tool:routines",
        "tool": "routines",
        "tool_args": args,
    }
    return {
        "id": delta_id,
        "tags": [
            "feed-card", "kind:proposal", "tool:routines",
            "action:create", "proposal-status:pending",
            "route:tool:routines",
        ],
        "content": json.dumps(payload),
        "timestamp": "2026-04-30T10:00:00Z",
    }


@pytest.fixture(autouse=True)
def _bypass_admin():
    """auth.require_admin → noop, via dependency_overrides."""
    from api import auth
    from api.server import app

    def _noop():
        return {"slug": "test", "role": "admin"}

    app.dependency_overrides[auth.require_admin] = _noop
    yield
    app.dependency_overrides.pop(auth.require_admin, None)


@pytest.mark.asyncio
async def test_get_returns_400_for_non_proposal(client, monkeypatch):
    from api import delta_client

    async def _get(_id):
        return {"id": "x", "tags": ["feed-card"], "content": "{}"}

    monkeypatch.setattr(delta_client, "get_delta", _get)

    r = await client.get("/v1/proposals/x")
    assert r.status_code == 400
    assert "not a proposal" in r.json()["detail"]


@pytest.mark.asyncio
async def test_get_returns_proposal_with_args(client, monkeypatch):
    from api import delta_client

    card = _proposal_card()

    async def _get(_id):
        return card

    async def _q(**_kwargs):
        return []

    monkeypatch.setattr(delta_client, "get_delta", _get)
    monkeypatch.setattr(delta_client, "query", _q)

    r = await client.get("/v1/proposals/p-1")
    assert r.status_code == 200
    data = r.json()
    assert data["tool"] == "routines"
    assert data["action"] == "create"
    assert data["tool_args"]["id"] == "ramen-check"
    assert data["decision"] is None


@pytest.mark.asyncio
async def test_approve_calls_routines_create(client, monkeypatch):
    from api import delta_client
    from api import routines as routines_mod

    card = _proposal_card()
    created: list[dict] = []
    decisions: list[dict] = []

    async def _get(_id):
        return card

    async def _create(body):
        created.append(body)
        return {"created": True, "routine_id": body["id"], "delta_id": "spec-1"}

    async def _write(**kwargs):
        decisions.append(kwargs)
        return {"id": "decision-1"}

    monkeypatch.setattr(delta_client, "get_delta", _get)
    monkeypatch.setattr(routines_mod, "create", _create)
    monkeypatch.setattr(delta_client, "write", _write)

    r = await client.post("/v1/proposals/p-1/approve")

    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is True
    assert body["tool"] == "routines"
    assert body["action"] == "create"
    assert body["result"]["routine_id"] == "ramen-check"
    assert created and created[0]["id"] == "ramen-check"
    assert "action" not in created[0]
    assert decisions
    tags = decisions[0]["tags"]
    assert "proposal-decision" in tags
    assert "decides:p-1" in tags
    assert "proposal-status:approved" in tags


@pytest.mark.asyncio
async def test_approve_with_edited_args(client, monkeypatch):
    """User clicks Edit → changes the schedule → Approves with new args."""
    from api import delta_client
    from api import routines as routines_mod

    card = _proposal_card()
    received: list[dict] = []

    async def _get(_id):
        return card

    async def _create(body):
        received.append(body)
        return {"created": True, "routine_id": body["id"], "delta_id": "spec-1"}

    async def _write(**_kwargs):
        return {"id": "d-1"}

    monkeypatch.setattr(delta_client, "get_delta", _get)
    monkeypatch.setattr(routines_mod, "create", _create)
    monkeypatch.setattr(delta_client, "write", _write)

    edited = {
        "action": "create",
        "id": "ramen-check",
        "name": "Menya Rui hours check",
        "schedule": "0 11 * * *",
        "prompt": "check if Menya Rui is open",
        "host": "fedora",
    }
    r = await client.post(
        "/v1/proposals/p-1/approve",
        json={"tool_args": edited},
    )
    assert r.status_code == 200
    assert received and received[0]["schedule"] == "0 11 * * *"


@pytest.mark.asyncio
async def test_deny_writes_decision_only(client, monkeypatch):
    from api import delta_client

    card = _proposal_card()
    decisions: list[dict] = []

    async def _get(_id):
        return card

    async def _write(**kwargs):
        decisions.append(kwargs)
        return {"id": "decision-1"}

    monkeypatch.setattr(delta_client, "get_delta", _get)
    monkeypatch.setattr(delta_client, "write", _write)

    r = await client.post(
        "/v1/proposals/p-1/deny",
        json={"reason": "not now"},
    )
    assert r.status_code == 200
    assert r.json()["denied"] is True
    assert decisions
    tags = decisions[0]["tags"]
    assert "proposal-status:denied" in tags
    body = json.loads(decisions[0]["content"])
    assert body["status"] == "denied"
    assert body["reason"] == "not now"
