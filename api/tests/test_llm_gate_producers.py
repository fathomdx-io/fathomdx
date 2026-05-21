"""Tests that background producers skip silently when llm_gate.is_down.

Each producer is gated on a specific tier (medium for mood/witness/
sediment/search-planner; hard for feed-orient). When that tier reports
down, the producer must:

  1. Skip the LLM call entirely (no chat.completions.create invoked)
  2. Return its safe fallback (None, fallback axes, False, etc.)
  3. NOT write a fresh error delta — the banner already tells the
     user; we're not here to add noise.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── mood synth ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mood_synth_skips_when_medium_down():
    from api import mood
    from api.loop import llm_gate

    with (
        patch.object(llm_gate, "is_down", AsyncMock(return_value=True)),
        patch.object(mood, "compute_topology", AsyncMock(return_value={"axes": []})),
        patch.object(mood, "_fetch_prior_mood", AsyncMock(return_value={"state": "neutral"})),
        patch.object(mood, "_fetch_recent_activity", AsyncMock(return_value="")),
        patch.object(mood, "_format_topology_for_prompt", return_value="some topology"),
        patch.object(mood, "delta_client") as fake_dc,
    ):
        fake_dc.write = AsyncMock()
        result = await mood.synthesize_mood()
    assert result is None
    # Critical: no error delta written either — the gate's job is to
    # cut noise, not produce new error rows of its own.
    fake_dc.write.assert_not_called()


@pytest.mark.asyncio
async def test_mood_synth_passes_correct_tier_to_gate():
    """Mood is medium-tier — make sure we're not accidentally checking
    a different tier."""
    from api import mood
    from api.loop import llm_gate

    captured_tier = {}

    async def fake_is_down(tier):
        captured_tier["tier"] = tier
        return True

    with (
        patch.object(llm_gate, "is_down", side_effect=fake_is_down),
        patch.object(mood, "compute_topology", AsyncMock(return_value={"axes": []})),
        patch.object(mood, "_fetch_prior_mood", AsyncMock(return_value={"state": "neutral"})),
        patch.object(mood, "_fetch_recent_activity", AsyncMock(return_value="")),
        patch.object(mood, "_format_topology_for_prompt", return_value="some topology"),
    ):
        await mood.synthesize_mood()
    assert captured_tier.get("tier") == "medium"


# ── witness judge ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_witness_judge_returns_fallback_when_medium_down():
    from api.loop import llm_gate, witness

    with patch.object(llm_gate, "is_down", AsyncMock(return_value=True)):
        axes = await witness._call_judge(kicker="K", body="B", seed="S")
    # All five axes should be at fallback values, not LLM-derived.
    assert axes == witness._JUDGE_FALLBACK
    assert axes is not witness._JUDGE_FALLBACK  # returned a copy, not the constant


# ── feed-orient ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feed_orient_skips_when_hard_down():
    from api.loop import feed_orient, llm_gate

    # _run_regen reads many state things; mock the small surface it
    # touches before hitting the gate check.
    with (
        patch.object(llm_gate, "is_down", AsyncMock(return_value=True)),
        patch.object(feed_orient, "_latest_feed_orient", AsyncMock(return_value=None)),
        patch.object(feed_orient, "_build_inputs_block", AsyncMock(return_value="")),
        patch.object(feed_orient, "loop_generate") as fake_llm,
    ):
        fake_llm.side_effect = AssertionError("LLM should not be called when gate is down")
        # _in_flight guard — reset just in case a prior test left it.
        feed_orient._in_flight = False
        result = await feed_orient._run_regen()
    assert result is False


# ── search planner ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_planner_returns_none_when_medium_down():
    from api import search
    from api.loop import llm_gate

    # The gate check happens before the local llm_config import, so we
    # only need to mock is_down — if the planner ever reaches resolve_tier
    # it would crash on the real lake call, signaling the gate didn't fire.
    with patch.object(llm_gate, "is_down", AsyncMock(return_value=True)):
        result = await search._generate_plan("any query")
    assert result is None


@pytest.mark.asyncio
async def test_search_deep_falls_back_to_shallow_when_planner_returns_none():
    """When the planner says None (gate down or other failure), deep
    recall must still return usable results via shallow."""
    from api import search

    shallow_result = {
        "deltas": [{"id": "d1"}],
        "plan": {"steps": []},
        "tree": {},
        "thinking_prose": None,
    }

    with (
        patch.object(search, "_generate_plan", AsyncMock(return_value=None)),
        patch.object(search, "_shallow", AsyncMock(return_value=shallow_result)) as fake_shallow,
    ):
        result = await search._deep(
            "query text",
            conv_context="",
            session_slug=None,
            limit=10,
            view="deltas",
        )
    fake_shallow.assert_called_once()
    assert result == shallow_result


# ── sediment synthesis ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sediment_synthesis_skips_when_medium_down():
    from api import search
    from api.loop import llm_gate

    # Need enough source ids that the MIN-DELTAS guard doesn't short-
    # circuit first. The exact ids/structure don't matter for this test.
    deltas_by_step = {
        "step1": [{"id": f"d{i}", "content": "x", "tags": []} for i in range(20)],
    }
    with patch.object(llm_gate, "is_down", AsyncMock(return_value=True)):
        prose, sid = await search._synthesize_thinking("q", deltas_by_step)
    assert prose is None
    assert sid is None
