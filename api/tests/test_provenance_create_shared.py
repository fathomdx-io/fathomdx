"""The shared provenance-creation path both surfaces funnel through.

`propose_and_autoapprove_provenance` is what the harness tool and the
`POST /v1/provenance` endpoint call. These pin its gate — bad input is
rejected with a `ProvenanceValidationError` (which each surface maps to
an ERROR string / 400) and never reaches the lake, and a valid request
drafts a proposal and auto-approves it into a real provenance delta.

The lake round-trips are mocked; the centroid math itself is covered by
test_provenance_centroid.py and the write wiring by
test_provenance_write_centroid.py. Here we only test the orchestration
and the validation boundary.
"""

from __future__ import annotations

import pytest

from api import provenance_centroid
from api import provenance_create as pc
from api.loop import puddle as puddle_mod


async def _resolvable_base(fid: str) -> dict:
    """Every id resolves to a plain base moment (no provenance-level tag),
    so the structural level-floor lands at 1."""
    return {"id": fid, "tags": ["kind:thread-msg"], "embedding": [0.1] * 512}


def _wire_lake(monkeypatch: pytest.MonkeyPatch, *, resolve: bool = True) -> dict:
    """Mock the lake so the happy path completes without a real store.
    Returns a dict recording what was written."""
    recorded: dict = {"writes": [], "centroid_called_with": None}

    async def fake_get_delta(fid):
        return await _resolvable_base(fid) if resolve else None

    async def fake_write(**kwargs):
        recorded["writes"].append(kwargs)
        return {"id": f"delta-{len(recorded['writes'])}"}

    async def fake_centroid(from_ids):
        recorded["centroid_called_with"] = list(from_ids)
        return [0.5] * provenance_centroid.VECTOR_DIM

    async def fake_puddle_write(**kwargs):
        return {"id": "puddle"}

    monkeypatch.setattr(pc.delta_client, "get_delta", fake_get_delta)
    monkeypatch.setattr(pc.delta_client, "write", fake_write)
    monkeypatch.setattr(provenance_centroid, "compute_centroid", fake_centroid)
    monkeypatch.setattr(puddle_mod.puddle, "write", fake_puddle_write)
    return recorded


# ── Validation boundary (no lake reached) ──────────────────────────────


@pytest.mark.asyncio
async def test_non_list_from_ids_rejected() -> None:
    with pytest.raises(pc.ProvenanceValidationError, match="from_ids must be a list"):
        await pc.propose_and_autoapprove_provenance(
            level=1, title="t", summary="s", from_ids="aaaaaaaaaaaa"  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_empty_from_ids_rejected_by_floor() -> None:
    with pytest.raises(pc.ProvenanceValidationError, match="needs at least 2"):
        await pc.propose_and_autoapprove_provenance(
            level=1, title="t", summary="s", from_ids=[]
        )


@pytest.mark.asyncio
async def test_missing_title_rejected() -> None:
    with pytest.raises(pc.ProvenanceValidationError, match="title is required"):
        await pc.propose_and_autoapprove_provenance(
            level=1, title="", summary="s", from_ids=["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
        )


@pytest.mark.asyncio
async def test_level_above_cap_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A level beyond the schema cap is caller error — resolvable ids so
    we reach the level check rather than the id-resolution guard."""
    _wire_lake(monkeypatch, resolve=True)
    with pytest.raises(pc.ProvenanceValidationError, match="level must be 1-3"):
        await pc.propose_and_autoapprove_provenance(
            level=7,
            title="t",
            summary="s",
            # 3 ids so the L2+ min-constituents floor passes and we reach
            # the level-cap check rather than the floor check.
            from_ids=["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"],
        )


@pytest.mark.asyncio
async def test_hallucinated_ids_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ids that don't resolve to real deltas are refused before any
    provenance is written — the anti-hallucination guard."""
    _wire_lake(monkeypatch, resolve=False)
    with pytest.raises(pc.ProvenanceValidationError, match="do not resolve"):
        await pc.propose_and_autoapprove_provenance(
            level=1, title="t", summary="s", from_ids=["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
        )


# ── Happy path — draft + auto-approve ──────────────────────────────────


@pytest.mark.asyncio
async def test_valid_request_drafts_and_autoapproves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _wire_lake(monkeypatch, resolve=True)

    result = await pc.propose_and_autoapprove_provenance(
        level=1,
        title="Trapped, two ways free",
        summary="Wojtek and Houdini — two escapes.",
        from_ids=["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
        rationale="both are escapes from confinement",
        produced_by="episodic-agent-pim",
        source="mcp-provenance",
    )

    assert result["auto_approved"] is True
    assert result["auto_error"] == ""
    assert result["level"] == 1
    assert result["from_count"] == 2
    assert result["proposal_id"]  # the proposal card landed
    assert result["provenance_delta_id"]  # the real provenance landed

    # The proposal card carries the caller's producer, not "harness".
    proposal_write = recorded["writes"][0]
    assert "produced-by:episodic-agent-pim" in proposal_write["tags"]
    assert "kind:proposal" in proposal_write["tags"]
    assert "provenance-level:1" in proposal_write["tags"]
    # The centroid was computed over exactly the constituents given.
    assert recorded["centroid_called_with"] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]


@pytest.mark.asyncio
async def test_unspecified_level_infers_from_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Level omitted, all children are base moments → the node lands at
    L1 (one above the -1 base-moment sentinel)."""
    _wire_lake(monkeypatch, resolve=True)
    result = await pc.propose_and_autoapprove_provenance(
        level=None,
        title="A pair",
        summary="two resonant deltas",
        from_ids=["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
    )
    assert result["level"] == 1
