"""Phase 5a tests — voice stances drift from frozen list to lake.

The closed loop: lake-stored kind:voice-stance deltas (latest per
voice) override the static fallback at convener fire time. Domain
voices that earn stances surface alongside the trimurti.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from api.loop import voice_stances as vs_mod
from api.loop.voice_stances import (
    _load_lake_stances,
    get_voice_stances,
    regenerate_voice_stance,
)


def _stance_delta(voice_name: str, stance: str, bias: str, ts: str = "2026-04-29T14:00:00Z") -> dict:
    return {
        "id": f"stance-{voice_name}",
        "tags": ["kind:voice-stance", f"voice:{voice_name}"],
        "content": json.dumps({"stance": stance, "bias": bias}),
        "timestamp": ts,
    }


# ── Read path ──────────────────────────────────────────────────────


def test_get_voice_stances_falls_back_to_static_when_lake_empty() -> None:
    """No lake stances → returns the static dyad unchanged."""

    async def _fake_query(**kwargs):
        return []

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(get_voice_stances())
    assert len(out) == 2
    names = [v["name"] for v in out]
    assert names == ["creator", "preserver"]
    # Static stance text is the prompts.py default
    assert "what new pattern wants to emerge" in out[0]["stance"]


def test_get_voice_stances_uses_lake_when_available() -> None:
    """Lake stance overrides static for that voice; others stay
    static."""
    lake_stance = "drifted creator stance from lake"
    lake_bias = "drifted creator bias"

    async def _fake_query(**kwargs):
        return [_stance_delta("creator", lake_stance, lake_bias)]

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(get_voice_stances())
    by_name = {v["name"]: v for v in out}
    assert by_name["creator"]["stance"] == lake_stance
    assert by_name["creator"]["bias"] == lake_bias
    # Preserver keeps static
    assert "what existing structure should be defended" in by_name["preserver"]["stance"]


def test_get_voice_stances_appends_domain_voices_after_trimurti() -> None:
    """Lake stances for non-trimurti voices show up at the end."""

    async def _fake_query(**kwargs):
        return [
            _stance_delta("compassion", "compassion stance", "softness bias"),
            _stance_delta("honesty", "honesty stance", "harshness bias"),
        ]

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(get_voice_stances())
    names = [v["name"] for v in out]
    # Static dyad first
    assert names[:2] == ["creator", "preserver"]
    # Domain voices alphabetical after
    assert names[2:] == ["compassion", "honesty"]


def test_get_voice_stances_latest_per_voice_wins() -> None:
    """Multiple stance deltas for the same voice — newest wins (rows
    arrive newest-first by default)."""

    async def _fake_query(**kwargs):
        return [
            _stance_delta("creator", "newer stance", "newer bias"),
            _stance_delta("creator", "older stance", "older bias"),
        ]

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(get_voice_stances())
    creator = next(v for v in out if v["name"] == "creator")
    assert creator["stance"] == "newer stance"


def test_get_voice_stances_skips_unparseable_content() -> None:
    """A malformed stance delta is dropped; voice falls back to
    static."""

    async def _fake_query(**kwargs):
        return [
            {
                "id": "x",
                "tags": ["kind:voice-stance", "voice:creator"],
                "content": "not json",
                "timestamp": "t",
            }
        ]

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(get_voice_stances())
    creator = next(v for v in out if v["name"] == "creator")
    # Static stance, not the malformed one
    assert "what new pattern wants to emerge" in creator["stance"]


def test_load_lake_stances_soft_fails_on_query_error() -> None:
    async def _fake_query(**kwargs):
        raise RuntimeError("lake down")

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(_load_lake_stances())
    assert out == {}


# ── Regen path ─────────────────────────────────────────────────────


def test_regenerate_voice_stance_returns_none_on_no_affirmations() -> None:
    """Without recent affirmations there's nothing to refine from —
    return None and skip the lake write."""

    async def _fake_query(tags_include=None, **kwargs):
        return []  # No affirmations

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(regenerate_voice_stance("creator"))
    assert out is None


def test_regenerate_voice_stance_returns_none_for_unknown_voice() -> None:
    """A voice with no static fallback and no lake history can't
    be regenerated — convener mints these on the fly anyway."""

    async def _fake_query(**kwargs):
        return []

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(regenerate_voice_stance("brand-new-domain-voice"))
    assert out is None


def test_regenerate_voice_stance_writes_when_llm_returns_change() -> None:
    """Full happy path: affirmations + cited cards + LLM refinement
    → new stance delta written."""
    affirmation = {
        "id": "aff1",
        "tags": ["kind:voice-affirmation", "voice:creator", "from:cardABC"],
        "content": "voice affirmed",
        "timestamp": "t",
    }
    card = {
        "id": "cardABC",
        "content": json.dumps({"body": "the witness card body about the topic"}),
    }
    fake_llm_response = json.dumps(
        {
            "stance": "drifted creator stance — the refined version",
            "bias": "drifted creator bias",
        }
    )

    async def _fake_query(tags_include=None, **kwargs):
        if tags_include and "kind:voice-affirmation" in tags_include:
            return [affirmation]
        if tags_include and "kind:voice-stance" in tags_include:
            return []  # No current lake stance — fall back to static
        return []

    async def _fake_batch_get(ids):
        return [card]

    async def _fake_generate(**kwargs):
        return fake_llm_response

    write_mock = AsyncMock(return_value={"id": "new-stance-id"})

    with patch.object(vs_mod.delta_client, "query", _fake_query), patch.object(
        vs_mod.delta_client, "batch_get", _fake_batch_get
    ), patch("api.loop.voice_stances.loop_generate", _fake_generate), patch.object(
        vs_mod.delta_client, "write", write_mock
    ):
        out = asyncio.run(regenerate_voice_stance("creator"))
    assert out == "new-stance-id"
    assert write_mock.called
    written = write_mock.call_args.kwargs
    assert "kind:voice-stance" in written["tags"]
    assert "voice:creator" in written["tags"]


def test_regenerate_voice_stance_no_op_when_unchanged() -> None:
    """LLM returned the same stance text as the current — skip the
    write to avoid lake clutter."""
    from api.loop.prompts import VOICES as STATIC

    static_creator = next(v for v in STATIC if v["name"] == "creator")
    affirmation = {
        "id": "aff1",
        "tags": ["kind:voice-affirmation", "voice:creator", "from:cardABC"],
        "content": "x",
        "timestamp": "t",
    }
    card = {"id": "cardABC", "content": json.dumps({"body": "x"})}
    # LLM returns the EXACT static text (no refinement)
    fake_llm_response = json.dumps(
        {"stance": static_creator["stance"], "bias": static_creator["bias"]}
    )

    async def _fake_query(tags_include=None, **kwargs):
        if tags_include and "kind:voice-affirmation" in tags_include:
            return [affirmation]
        return []

    async def _fake_batch_get(ids):
        return [card]

    async def _fake_generate(**kwargs):
        return fake_llm_response

    write_mock = AsyncMock(return_value={"id": "x"})

    with patch.object(vs_mod.delta_client, "query", _fake_query), patch.object(
        vs_mod.delta_client, "batch_get", _fake_batch_get
    ), patch("api.loop.voice_stances.loop_generate", _fake_generate), patch.object(
        vs_mod.delta_client, "write", write_mock
    ):
        out = asyncio.run(regenerate_voice_stance("creator"))
    assert out is None
    assert not write_mock.called


# ── Watcher eligibility ─────────────────────────────────────────────


def _aff(voice: str, ts: str) -> dict:
    return {
        "id": "x",
        "tags": ["kind:voice-affirmation", f"voice:{voice}"],
        "timestamp": ts,
    }


def test_watcher_picks_voice_with_most_recent_affirmations_above_gate() -> None:
    """Voice with >= gate affirmations newer than its last stance is
    eligible. Highest count wins."""
    affirmations = (
        [_aff("creator", f"2026-04-29T{i:02d}:00:00Z") for i in range(20, 27)]
        + [_aff("preserver", f"2026-04-29T{i:02d}:00:00Z") for i in range(20, 23)]
    )

    async def _fake_query(tags_include=None, **kwargs):
        if tags_include and "kind:voice-affirmation" in tags_include:
            return affirmations
        # No prior stance writes — eligibility is automatic
        return []

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(vs_mod._voices_eligible_for_regen())
    # creator has 7 (>= 5 gate); preserver has 3 (< gate). Only creator
    # surfaces.
    assert out == ["creator"]


def test_watcher_skips_voices_below_gate() -> None:
    """A voice with affirmations but below the gate is not eligible."""
    affirmations = [_aff("creator", f"2026-04-29T{i:02d}:00:00Z") for i in range(20, 23)]

    async def _fake_query(tags_include=None, **kwargs):
        if tags_include and "kind:voice-affirmation" in tags_include:
            return affirmations
        return []

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(vs_mod._voices_eligible_for_regen())
    # 3 affirmations < gate of 5
    assert out == []


def test_watcher_skips_voices_with_recent_stance_write() -> None:
    """If the voice's latest kind:voice-stance is newer than its
    latest affirmation, no regen needed — we already drifted."""
    affirmations = [_aff("creator", "2026-04-29T10:00:00Z") for _ in range(7)]

    async def _fake_query(tags_include=None, **kwargs):
        if tags_include and "kind:voice-affirmation" in tags_include:
            return affirmations
        if tags_include and "kind:voice-stance" in tags_include:
            return [
                _stance_delta(
                    "creator",
                    "already drifted stance",
                    "already drifted bias",
                    ts="2026-04-29T15:00:00Z",  # newer than affirmations
                )
            ]
        return []

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(vs_mod._voices_eligible_for_regen())
    assert out == []


def test_watcher_returns_empty_on_lake_error() -> None:
    async def _fake_query(**kwargs):
        raise RuntimeError("lake down")

    with patch.object(vs_mod.delta_client, "query", _fake_query):
        out = asyncio.run(vs_mod._voices_eligible_for_regen())
    assert out == []


def test_regenerate_voice_stance_handles_llm_malformed_json() -> None:
    affirmation = {
        "id": "aff1",
        "tags": ["kind:voice-affirmation", "voice:creator", "from:cardABC"],
        "content": "x",
        "timestamp": "t",
    }
    card = {"id": "cardABC", "content": json.dumps({"body": "x"})}

    async def _fake_query(tags_include=None, **kwargs):
        if tags_include and "kind:voice-affirmation" in tags_include:
            return [affirmation]
        return []

    async def _fake_batch_get(ids):
        return [card]

    async def _fake_generate(**kwargs):
        return "this is not valid json"

    write_mock = AsyncMock()

    with patch.object(vs_mod.delta_client, "query", _fake_query), patch.object(
        vs_mod.delta_client, "batch_get", _fake_batch_get
    ), patch("api.loop.voice_stances.loop_generate", _fake_generate), patch.object(
        vs_mod.delta_client, "write", write_mock
    ):
        out = asyncio.run(regenerate_voice_stance("creator"))
    assert out is None
    assert not write_mock.called
