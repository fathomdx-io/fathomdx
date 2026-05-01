"""Tests for /v1/routines/<id>/rewrite-to-schema and the river-routed
default of /v1/routines/<id>/fire."""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _bypass_admin():
    from api import auth
    from api.server import app

    def _noop():
        return {"slug": "test", "role": "admin"}

    app.dependency_overrides[auth.require_admin] = _noop
    yield
    app.dependency_overrides.pop(auth.require_admin, None)


def _spec(rid="ramen", body="check ramen hours and tell me if open", **meta_overrides):
    meta = {
        "id": rid,
        "name": "Ramen Check",
        "schedule": "0 12 * * *",
        "host": "fedora",
        "enabled": True,
        "deleted": False,
        **meta_overrides,
    }
    return {"delta": {"id": "spec-x"}, "meta": meta, "body": body, "workspace": ""}


@pytest.mark.asyncio
async def test_rewrite_skips_when_already_in_schema(client, monkeypatch):
    from api import routines as routines_mod

    body = (
        "# Purpose\nx\n\n# Needs\nclaude-code on fedora\n\n"
        "# Steps\n1. y\n\n# Ending\ncard\n"
    )

    async def _spec_fn(_rid):
        return _spec(body=body)

    monkeypatch.setattr(routines_mod, "get_latest_spec", _spec_fn)

    r = await client.post("/v1/routines/ramen/rewrite-to-schema")
    assert r.status_code == 200
    data = r.json()
    assert data["skipped"] is True
    assert data["reason"] == "already-in-schema"


@pytest.mark.asyncio
async def test_rewrite_emits_proposal(client, monkeypatch):
    from api import delta_client, routines as routines_mod
    from api.loop import llm

    async def _spec_fn(_rid):
        return _spec()

    rewritten = (
        "# Purpose\nCheck ramen hours.\n\n"
        "# Needs\nclaude-code on fedora.\n\n"
        "# Steps\n1. Look up Menya Rui.\n\n"
        "# Ending\nSoft alert if open and closing soon.\n"
    )

    async def _llm(*, prompt, tier="medium", max_tokens=200, temperature=0.95,
                    json_mode=False, max_retries=4):
        return rewritten

    writes: list[dict] = []

    async def _write(content, tags=None, source="x", expires_at=None, media_hash=None):
        writes.append({"content": content, "tags": tags, "source": source})
        return {"id": "proposal-1"}

    monkeypatch.setattr(routines_mod, "get_latest_spec", _spec_fn)
    monkeypatch.setattr(llm, "loop_generate", _llm)
    monkeypatch.setattr(delta_client, "write", _write)

    r = await client.post("/v1/routines/ramen/rewrite-to-schema")
    assert r.status_code == 200
    data = r.json()
    assert data["proposed"] is True
    assert data["proposal_delta_id"] == "proposal-1"

    assert len(writes) == 1
    tags = writes[0]["tags"]
    assert "kind:proposal" in tags
    assert "tool:routines" in tags
    assert "action:update" in tags
    assert "rewrite-to-schema" in tags
    payload = json.loads(writes[0]["content"])
    assert payload["tool"] == "routines"
    assert payload["tool_args"]["action"] == "update"
    assert payload["tool_args"]["id"] == "ramen"
    assert "# Purpose" in payload["tool_args"]["prompt"]


@pytest.mark.asyncio
async def test_rewrite_404_for_missing_routine(client, monkeypatch):
    from api import routines as routines_mod

    async def _spec_fn(_rid):
        return None

    monkeypatch.setattr(routines_mod, "get_latest_spec", _spec_fn)

    r = await client.post("/v1/routines/missing/rewrite-to-schema")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_fire_now_routes_through_river(client, monkeypatch):
    """Fire Now delegates to routines.fire(), which writes a routine-due
    intent + a routine-tick marker. There's no longer a "skip the river"
    override — the legacy via=direct path was retired."""
    from api import routines as routines_mod

    fire_calls: list[tuple] = []

    async def _fire(rid, prompt_override=None):
        fire_calls.append((rid, prompt_override))
        return {"fired": True, "routine_id": rid, "intent_id": "intent-1"}

    monkeypatch.setattr(routines_mod, "fire", _fire)

    r = await client.post("/v1/routines/ramen/fire", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["fired"] is True
    assert body["intent_id"] == "intent-1"
    assert fire_calls == [("ramen", None)]


@pytest.mark.asyncio
async def test_fire_now_404_for_missing_routine(client, monkeypatch):
    from api import routines as routines_mod

    async def _fire(rid, prompt_override=None):
        raise FileNotFoundError(f"Routine {rid} not found")

    monkeypatch.setattr(routines_mod, "fire", _fire)

    r = await client.post("/v1/routines/missing/fire", json={})
    assert r.status_code == 404
