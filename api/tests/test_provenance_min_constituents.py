"""Per-level minimum-constituents floor for propose_provenance.

L1 accepts 2+ tightly-related deltas (a resonant pair is already a
stretch worth naming). L2+ needs 3+ child stretches (topic / era
groups multiple episodes; a pair belongs at L1).

Pins both ends so a future change to the floor surfaces here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.loop.harness import tools as harness_tools


@pytest.mark.asyncio
async def test_l1_accepts_two_constituents():
    """L1 with 2 constituents is allowed by the floor; the call may
    still fail later validation (delta-resolution etc.) but NOT for
    "needs at least N constituents"."""
    # Mock batch_get to return both ids as resolvable so the call
    # progresses past resolution. We don't actually persist — patching
    # delta_client.write at the end short-circuits the lake side.
    rows = [
        {
            "id": "aaaaaaaaaaaa",
            "tags": ["kind:thread-msg"],
            "timestamp": "2026-05-05T12:00:00+00:00",
        },
        {
            "id": "bbbbbbbbbbbb",
            "tags": ["kind:thread-msg"],
            "timestamp": "2026-05-05T12:00:01+00:00",
        },
    ]
    with (
        patch.object(harness_tools.delta_client, "batch_get", AsyncMock(return_value=rows)),
        patch.object(harness_tools.delta_client, "write", AsyncMock(return_value={"id": "prov-1"})),
        patch.object(harness_tools.puddle, "write", AsyncMock(return_value={"id": "p"})),
    ):
        result = await harness_tools.tool_propose_provenance(
            level=1,
            title="Test Pair",
            summary="A pair of resonant deltas worth naming.",
            from_ids=["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
            rationale="they cluster",
        )
    assert "needs at least" not in result, f"L1 with 2 constituents was rejected: {result}"


@pytest.mark.asyncio
async def test_l1_rejects_one_constituent():
    """L1 with 1 constituent is below the floor."""
    result = await harness_tools.tool_propose_provenance(
        level=1,
        title="Single",
        summary="just one",
        from_ids=["aaaaaaaaaaaa"],
        rationale="x",
    )
    assert "needs at least 2" in result
    assert "L1" in result


@pytest.mark.asyncio
async def test_l2_rejects_two_constituents():
    """L2 still needs 3 — a topic groups multiple stretches."""
    result = await harness_tools.tool_propose_provenance(
        level=2,
        title="Pair Topic",
        summary="two episodes",
        from_ids=["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
        rationale="x",
    )
    assert "needs at least 3" in result
    assert "L2" in result


@pytest.mark.asyncio
async def test_l3_rejects_two_constituents():
    """L3 also needs 3."""
    result = await harness_tools.tool_propose_provenance(
        level=3,
        title="Pair Era",
        summary="two topics",
        from_ids=["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
        rationale="x",
    )
    assert "needs at least 3" in result
    assert "L3" in result


@pytest.mark.asyncio
async def test_unspecified_level_treated_as_l1():
    """When the model omits level, the floor defaults to L1's (2).
    Otherwise a thin 2-id proposal would get rejected on a benign
    omission."""
    rows = [
        {
            "id": "aaaaaaaaaaaa",
            "tags": ["kind:thread-msg"],
            "timestamp": "2026-05-05T12:00:00+00:00",
        },
        {
            "id": "bbbbbbbbbbbb",
            "tags": ["kind:thread-msg"],
            "timestamp": "2026-05-05T12:00:01+00:00",
        },
    ]
    with (
        patch.object(harness_tools.delta_client, "batch_get", AsyncMock(return_value=rows)),
        patch.object(harness_tools.delta_client, "write", AsyncMock(return_value={"id": "prov-1"})),
        patch.object(harness_tools.puddle, "write", AsyncMock(return_value={"id": "p"})),
    ):
        result = await harness_tools.tool_propose_provenance(
            level=None,
            title="Default",
            summary="x",
            from_ids=["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
            rationale="x",
        )
    # Should NOT be rejected by the constituents floor (the default
    # level resolves to 1 from base-moment children later in the flow).
    assert "needs at least" not in result, f"unspecified level rejected: {result}"
