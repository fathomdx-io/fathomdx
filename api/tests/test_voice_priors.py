"""Phase 4a tests — voice priors accumulate from past affirmations
and shape the convener's prompt on the next fire.

The closed-loop pattern: witness fire scores high → voice-affirmation
deltas land in lake → convener reads on next fire → biases voice
selection toward voices that earned standing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from api.loop import voice_priors as vp_mod
from api.loop.voice_priors import get_voice_priors, render_priors_for_prompt


# ── get_voice_priors ───────────────────────────────────────────────


def _affirmation_delta(voice_name: str, ts: str = "2026-04-29T14:00:00Z") -> dict:
    return {
        "id": "x",
        "tags": ["kind:voice-affirmation", f"voice:{voice_name}"],
        "timestamp": ts,
    }


def test_voice_priors_normalizes_to_top_score() -> None:
    """The most-affirmed voice scores 1.0; others scale relative."""
    rows = (
        [_affirmation_delta("creator")] * 10
        + [_affirmation_delta("preserver")] * 5
        + [_affirmation_delta("destroyer")] * 2
    )

    async def _fake_query(**kwargs):
        return rows

    with patch.object(vp_mod.delta_client, "query", _fake_query):
        priors = asyncio.run(get_voice_priors())
    assert priors["creator"] == 1.0
    assert priors["preserver"] == 0.5
    assert priors["destroyer"] == 0.2


def test_voice_priors_empty_lake_returns_empty_dict() -> None:
    async def _fake_query(**kwargs):
        return []

    with patch.object(vp_mod.delta_client, "query", _fake_query):
        priors = asyncio.run(get_voice_priors())
    assert priors == {}


def test_voice_priors_lake_error_returns_empty_dict() -> None:
    async def _fake_query(**kwargs):
        raise RuntimeError("lake down")

    with patch.object(vp_mod.delta_client, "query", _fake_query):
        priors = asyncio.run(get_voice_priors())
    assert priors == {}


def test_voice_priors_skips_deltas_without_voice_tag() -> None:
    """A delta tagged kind:voice-affirmation but missing voice:<name>
    should be skipped, not crash."""
    rows = [
        _affirmation_delta("creator"),
        {"id": "y", "tags": ["kind:voice-affirmation"], "timestamp": "t"},
    ]

    async def _fake_query(**kwargs):
        return rows

    with patch.object(vp_mod.delta_client, "query", _fake_query):
        priors = asyncio.run(get_voice_priors())
    assert priors == {"creator": 1.0}


def test_voice_priors_only_first_voice_tag_per_delta() -> None:
    """If a delta carries multiple voice tags (shouldn't happen, but
    defensive), only the first is counted — prevents one delta from
    inflating two voices' scores."""
    rows = [
        {
            "id": "x",
            "tags": ["kind:voice-affirmation", "voice:creator", "voice:destroyer"],
            "timestamp": "t",
        }
    ]

    async def _fake_query(**kwargs):
        return rows

    with patch.object(vp_mod.delta_client, "query", _fake_query):
        priors = asyncio.run(get_voice_priors())
    # Only creator gets credit (first tag wins).
    assert priors == {"creator": 1.0}


# ── render_priors_for_prompt ───────────────────────────────────────


def test_render_priors_descending_by_score() -> None:
    out = render_priors_for_prompt({"a": 0.4, "b": 1.0, "c": 0.7})
    assert out.index("b (standing 1.00)") < out.index("c (standing 0.70)")
    assert out.index("c (standing 0.70)") < out.index("a (standing 0.40)")


def test_render_priors_skips_below_noise_floor() -> None:
    """Voices with score < 0.2 are noise — one-off fires that don't
    represent real standing. They get filtered."""
    out = render_priors_for_prompt({"creator": 1.0, "rare": 0.1})
    assert "creator" in out
    assert "rare" not in out


def test_render_priors_empty_dict_returns_empty_string() -> None:
    assert render_priors_for_prompt({}) == ""


def test_render_priors_all_below_floor_returns_empty() -> None:
    assert render_priors_for_prompt({"a": 0.05, "b": 0.1}) == ""
