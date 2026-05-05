"""Tests for the provenance-created bell alert path.

Pins the projection from kind:provenance lake delta → bell alert
shape, the viewed_at filter (read-driven, ages past on ack), and
the integration with the existing pending-proposal + witness-card
alert sources.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.routes import alerts


def _prov(*, did: str, level: int = 1, ts: str = "2026-05-05T12:00:00+00:00",
          title: str = "Test Provenance", body: str = "Summary text.",
          from_count: int = 5):
    tags = ["kind:provenance", f"provenance-level:{level}"]
    for i in range(from_count):
        tags.append(f"from:source-delta-{i}")
    return {
        "id": did,
        "timestamp": ts,
        "tags": tags,
        "content": f"{title}\n\n{body}",
    }


# ── _recent_provenance_alerts ─────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_provenance_returns_alerts_when_unviewed():
    rows = [_prov(did="prov-1")]
    with patch.object(alerts.delta_client, "query", AsyncMock(return_value=rows)):
        out = await alerts._recent_provenance_alerts(viewed_at="")
    assert len(out) == 1
    assert out[0]["kind"] == "provenance-created"
    assert out[0]["delta_id"] == "prov-1"
    assert out[0]["level"] == 1
    assert "Test Provenance" in out[0]["title"]
    assert "5 constituents" in out[0]["preview"]


@pytest.mark.asyncio
async def test_recent_provenance_filters_by_viewed_at():
    """Items older than the viewer's last read-receipt drop out."""
    rows = [
        _prov(did="old", ts="2026-05-05T10:00:00+00:00"),
        _prov(did="fresh", ts="2026-05-05T13:00:00+00:00"),
    ]
    with patch.object(alerts.delta_client, "query", AsyncMock(return_value=rows)):
        out = await alerts._recent_provenance_alerts(viewed_at="2026-05-05T12:00:00+00:00")
    assert [a["delta_id"] for a in out] == ["fresh"]


@pytest.mark.asyncio
async def test_recent_provenance_includes_level_in_title():
    rows = [_prov(did="x", level=2, title="Big Topic")]
    with patch.object(alerts.delta_client, "query", AsyncMock(return_value=rows)):
        out = await alerts._recent_provenance_alerts(viewed_at="")
    assert out[0]["title"] == "L2 created: Big Topic"


@pytest.mark.asyncio
async def test_recent_provenance_handles_lake_failure():
    """A lake hiccup must not crash the bell — return empty."""
    with patch.object(alerts.delta_client, "query", AsyncMock(side_effect=Exception("lake down"))):
        out = await alerts._recent_provenance_alerts(viewed_at="")
    assert out == []


@pytest.mark.asyncio
async def test_recent_provenance_skips_rows_without_id():
    rows = [{"id": "", "timestamp": "2026-05-05T12:00:00+00:00", "tags": ["kind:provenance"], "content": "x"}]
    with patch.object(alerts.delta_client, "query", AsyncMock(return_value=rows)):
        out = await alerts._recent_provenance_alerts(viewed_at="")
    assert out == []
