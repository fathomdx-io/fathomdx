"""Tests for alert dedup at the witness dispatch layer.

These pin the body-overlap heuristic so a chronic-broken substrate
can't loop the API account dry by re-emitting the same alert under
slightly different wording.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from api.loop import witness


# ── token extraction ──────────────────────────────────────────────────


def test_alert_tokens_drops_short_words():
    tokens = witness._alert_tokens("the cat sat on the mat")
    # All tokens must be >= 4 chars.
    assert all(len(t) >= 4 for t in tokens)


def test_alert_tokens_normalizes_case():
    a = witness._alert_tokens("Hard Problem Heartbeat")
    b = witness._alert_tokens("hard problem heartbeat")
    assert a == b


def test_alert_tokens_strips_punctuation():
    tokens = witness._alert_tokens("Routine `hard-problem` failed!")
    assert "routine" in tokens
    assert "hard" in tokens
    assert "problem" in tokens
    assert "failed" in tokens


def test_alert_tokens_empty_for_short_text():
    assert witness._alert_tokens("") == set()
    assert witness._alert_tokens("a") == set()


# ── _should_suppress_alert ────────────────────────────────────────────


def _make_alert_delta(body: str, delta_id: str = "abc123def456"):
    return {
        "id": delta_id,
        "tags": ["feed-card", "route:alert:critical"],
        "content": json.dumps({"kicker": "Alert", "title": "x", "body": body}),
    }


def _make_non_alert_delta(body: str):
    return {
        "id": "xxx",
        "tags": ["feed-card", "route:chat-reply"],
        "content": json.dumps({"body": body}),
    }


@pytest.mark.asyncio
async def test_no_suppress_when_no_recent_alerts():
    """Empty lake → first alert through."""
    with patch.object(witness.delta_client, "query", AsyncMock(return_value=[])):
        suppress, dup = await witness._should_suppress_alert(
            body="the hard-problem heartbeat routine has failed four times"
        )
    assert suppress is False
    assert dup == ""


@pytest.mark.asyncio
async def test_no_suppress_for_short_body():
    """Bodies with too few tokens can't be reliably compared — let through."""
    with patch.object(witness.delta_client, "query", AsyncMock(return_value=[])):
        suppress, dup = await witness._should_suppress_alert(body="oh no")
    assert suppress is False


@pytest.mark.asyncio
async def test_suppress_high_overlap_alert():
    """Two alert bodies with substantive shared vocabulary → suppress."""
    existing = _make_alert_delta(
        body=(
            "the hard-problem heartbeat routine has failed four consecutive "
            "times. the last attempt was rejected by the api credit shortage. "
            "investigation pending."
        ),
        delta_id="dup-id-123",
    )
    new_body = (
        "the hard-problem heartbeat routine has failed four times recently. "
        "the last attempt rejected because of api credit shortage. "
        "investigation needed."
    )
    with patch.object(witness.delta_client, "query", AsyncMock(return_value=[existing])):
        suppress, dup = await witness._should_suppress_alert(body=new_body)
    assert suppress is True
    assert dup == "dup-id-123"


@pytest.mark.asyncio
async def test_suppress_realworld_rephrasing_storm():
    """Regression: real captured alert storm where six near-identical
    cards landed in 82s. Pre-fix (0.7 threshold) let all six through;
    post-fix (0.4) catches them as duplicates. Bodies sit ~0.42 Jaccard
    — same nouns, divergent prose."""
    existing = _make_alert_delta(
        body=(
            "The 'Hard Problem Heartbeat' routine has failed in four recent "
            "attempts. Three of these failures were consistently caused by "
            "an Anthropic API credit shortage. This indicates a persistent "
            "operational issue requiring attention."
        ),
        delta_id="dup-real-1",
    )
    new_body = (
        "The `Hard Problem: Heartbeat` routine has failed four times "
        "recently. The first three of these failures were consistently "
        "blocked by an Anthropic API credit shortage during the `DECIDE` "
        "phase. This is preventing a core research routine from completing."
    )
    with patch.object(witness.delta_client, "query", AsyncMock(return_value=[existing])):
        suppress, dup = await witness._should_suppress_alert(body=new_body)
    assert suppress is True
    assert dup == "dup-real-1"


@pytest.mark.asyncio
async def test_no_suppress_low_overlap_alert():
    """Different content (overlap < threshold) → let through."""
    existing = _make_alert_delta(
        body="the hard-problem heartbeat routine has failed four times"
    )
    new_body = (
        "memory pressure climbing rapidly across multiple workspaces; "
        "swap thrashing detected on production database server. action urgent."
    )
    with patch.object(witness.delta_client, "query", AsyncMock(return_value=[existing])):
        suppress, dup = await witness._should_suppress_alert(body=new_body)
    assert suppress is False


@pytest.mark.asyncio
async def test_only_compares_against_alert_route_cards():
    """A near-identical chat-reply card in the recent window is NOT a
    duplicate. Only route:alert:* cards count."""
    existing_chat = _make_non_alert_delta(
        body="the hard-problem heartbeat routine has failed four consecutive times"
    )
    new_body = "the hard-problem heartbeat routine has failed four consecutive times"
    with patch.object(witness.delta_client, "query", AsyncMock(return_value=[existing_chat])):
        suppress, dup = await witness._should_suppress_alert(body=new_body)
    assert suppress is False


@pytest.mark.asyncio
async def test_query_failure_does_not_suppress():
    """If the lake query fails, default to letting the alert through.
    Better to over-alert than to swallow a real piercing condition."""
    with patch.object(
        witness.delta_client, "query", AsyncMock(side_effect=Exception("boom"))
    ):
        suppress, dup = await witness._should_suppress_alert(
            body="something is genuinely broken in the substrate, urgent"
        )
    assert suppress is False
