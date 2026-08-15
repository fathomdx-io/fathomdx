"""The stored provenance embedding is the CENTROID of its constituents,
not the embedding of its own title+summary text.

`_approve_provenance_create` is the single write point every provenance
funnels through — the harness auto-approve, the operator Approve button,
and the MCP `POST /v1/provenance` endpoint all reach it. This pins the
wiring that makes provenance real rather than a costume: the value handed
to `delta_client.write(provenance_embedding=...)` is exactly what
`compute_centroid(from_ids)` returned, and the title+summary text goes to
`content`, never to the semantic embedding. Get this wrong and provenance
is findable only by its label, defeating substance-based recall.
"""

from __future__ import annotations

import pytest

from api import provenance_centroid
from api.routes import proposals


@pytest.mark.asyncio
async def test_stored_embedding_is_constituent_centroid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_centroid = [0.5] * provenance_centroid.VECTOR_DIM
    captured: dict = {}

    async def fake_write(**kwargs):
        captured.update(kwargs)
        return {"id": "prov-new"}

    async def fake_centroid(from_ids):
        captured["centroid_from_ids"] = list(from_ids)
        return sentinel_centroid

    monkeypatch.setattr(proposals.delta_client, "write", fake_write)
    monkeypatch.setattr(provenance_centroid, "compute_centroid", fake_centroid)

    result = await proposals._approve_provenance_create(
        {
            "title": "Trapped, two ways free",
            "summary": "Wojtek and Houdini — two escapes from a box.",
            "level": 1,
            "from_ids": ["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
        },
        proposal=None,
        produced_by="episodic-agent",
    )

    # The embedding stored on the provenance row is the centroid...
    assert captured["provenance_embedding"] == sentinel_centroid
    # ...computed over exactly the from_ids the caller supplied...
    assert captured["centroid_from_ids"] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    # ...while the title+summary text went to `content`, NOT the embedding.
    assert "Trapped, two ways free" in captured["content"]
    assert "two escapes" in captured["content"]
    assert captured["provenance_embedding"] != captured["content"]
    # Kind + level + produced-by tags are what make it a genuine node.
    assert "kind:provenance" in captured["tags"]
    assert "provenance-level:1" in captured["tags"]
    assert "produced-by:episodic-agent" in captured["tags"]
    assert result["centroid_dim"] == provenance_centroid.VECTOR_DIM
    assert result["delta_id"] == "prov-new"


@pytest.mark.asyncio
async def test_missing_constituents_rejected_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No from_ids → ValueError, and nothing is written. A provenance
    with no constituents has no centroid to stand on."""
    wrote = False

    async def fake_write(**kwargs):
        nonlocal wrote
        wrote = True
        return {"id": "x"}

    monkeypatch.setattr(proposals.delta_client, "write", fake_write)

    with pytest.raises(ValueError, match="from_ids"):
        await proposals._approve_provenance_create(
            {"title": "t", "summary": "s", "level": 1, "from_ids": []},
            proposal=None,
        )
    assert wrote is False
