"""Phase 5b tests — convener reads recent judge axes per intent kind.

Aggregates from witness card payloads (axes inside content JSON).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from api.loop import judge_history as jh_mod
from api.loop.judge_history import (
    recent_judge_stats_by_kind,
    render_judge_history_for_prompt,
)


def _card(kind: str, axes: dict, ts: str = "2026-04-29T14:00:00Z") -> dict:
    return {
        "id": f"card-{kind}",
        "tags": [
            "synthesis",
            "addressing-output",
            f"kind:{kind}",
        ],
        "content": json.dumps({"axes": axes}),
        "timestamp": ts,
    }


# ── recent_judge_stats_by_kind ─────────────────────────────────────


def test_judge_stats_aggregates_per_kind() -> None:
    rows = [
        _card("reflection", {"salience": 0.6, "resonance": 0.7, "confidence": 0.8}),
        _card("reflection", {"salience": 0.5, "resonance": 0.6, "confidence": 0.7}),
        _card("question", {"salience": 0.9, "resonance": 0.8, "confidence": 0.9}),
    ]

    async def _fake_query(**kwargs):
        return rows

    with patch.object(jh_mod.delta_client, "query", _fake_query):
        out = asyncio.run(recent_judge_stats_by_kind(["reflection", "question"]))
    assert "reflection" in out
    assert "question" in out
    assert out["reflection"]["samples"] == 2
    # avg of (0.6, 0.5)
    assert abs(out["reflection"]["avg_salience"] - 0.55) < 0.01
    assert out["question"]["samples"] == 1
    assert out["question"]["avg_salience"] == 0.9


def test_judge_stats_empty_kinds_returns_empty() -> None:
    out = asyncio.run(recent_judge_stats_by_kind([]))
    assert out == {}


def test_judge_stats_unrequested_kinds_excluded() -> None:
    """A card tagged with a kind we didn't ask about is dropped."""
    rows = [
        _card("alert", {"salience": 0.5, "resonance": 0.5, "confidence": 0.5}),
    ]

    async def _fake_query(**kwargs):
        return rows

    with patch.object(jh_mod.delta_client, "query", _fake_query):
        out = asyncio.run(recent_judge_stats_by_kind(["question"]))
    assert out == {}


def test_judge_stats_lake_error_returns_empty() -> None:
    async def _fake_query(**kwargs):
        raise RuntimeError("lake down")

    with patch.object(jh_mod.delta_client, "query", _fake_query):
        out = asyncio.run(recent_judge_stats_by_kind(["question"]))
    assert out == {}


def test_judge_stats_unparseable_payload_skipped() -> None:
    """A card with malformed JSON content is skipped, not crashed."""
    bad = {
        "id": "x",
        "tags": ["synthesis", "addressing-output", "kind:question"],
        "content": "not json",
        "timestamp": "t",
    }

    async def _fake_query(**kwargs):
        return [
            bad,
            _card("question", {"salience": 0.5, "resonance": 0.5, "confidence": 0.5}),
        ]

    with patch.object(jh_mod.delta_client, "query", _fake_query):
        out = asyncio.run(recent_judge_stats_by_kind(["question"]))
    assert out["question"]["samples"] == 1


def test_judge_stats_caps_at_per_kind_limit() -> None:
    """Even if the lake returns 20 reflection cards, the aggregate
    uses at most _PER_KIND_LIMIT recent samples."""
    rows = [
        _card("reflection", {"salience": 0.5, "resonance": 0.5, "confidence": 0.5})
    ] * 20

    async def _fake_query(**kwargs):
        return rows

    with patch.object(jh_mod.delta_client, "query", _fake_query):
        out = asyncio.run(recent_judge_stats_by_kind(["reflection"]))
    assert out["reflection"]["samples"] == jh_mod._PER_KIND_LIMIT


# ── render_judge_history_for_prompt ─────────────────────────────────


def test_render_history_empty_dict_returns_empty_string() -> None:
    assert render_judge_history_for_prompt({}) == ""


def test_render_history_includes_per_kind_stats() -> None:
    stats = {
        "reflection": {
            "samples": 3,
            "avg_salience": 0.55,
            "avg_resonance": 0.65,
            "avg_confidence": 0.70,
        }
    }
    out = render_judge_history_for_prompt(stats)
    assert "reflection" in out
    assert "last 3" in out
    assert "0.55" in out
    assert "0.70" in out


def test_render_history_orders_kinds_alphabetical_for_stability() -> None:
    stats = {
        "z-kind": {"samples": 1, "avg_salience": 0.5, "avg_resonance": 0.5, "avg_confidence": 0.5},
        "a-kind": {"samples": 1, "avg_salience": 0.5, "avg_resonance": 0.5, "avg_confidence": 0.5},
    }
    out = render_judge_history_for_prompt(stats)
    assert out.index("a-kind") < out.index("z-kind")
