"""Unit tests for api.standpoint — the River's self-state read surface.

Phase 1 of the River refactor: this module is purely additive. Tests
pin the typed-object shape, the posture-inference rules, the renderer
budget, and the per-component soft-fail behavior so future stages
that thread Standpoint through can rely on a consistent contract.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from api import standpoint as sp_mod
from api.standpoint import (
    Affect,
    Endorsement,
    Identity,
    Sediment,
    Standpoint,
    _infer_posture,
    render_for_prompt,
)


# ── Posture inference ───────────────────────────────────────────────


def test_posture_tired_state_is_terse() -> None:
    assert _infer_posture(Identity(), Affect(state="tired")) == "terse"


def test_posture_wired_state_is_fast() -> None:
    assert _infer_posture(Identity(), Affect(state="wired")) == "fast"


def test_posture_settled_state_is_generous() -> None:
    assert _infer_posture(Identity(), Affect(state="settled")) == "generous"


def test_posture_unsettled_state_is_cautious() -> None:
    assert _infer_posture(Identity(), Affect(state="unsettled")) == "cautious"


def test_posture_unknown_state_falls_through_to_neutral() -> None:
    assert _infer_posture(Identity(), Affect(state="completely-novel")) == "neutral"


def test_posture_unset_state_with_no_identity_signal_is_neutral() -> None:
    assert _infer_posture(Identity(), Affect()) == "neutral"


# ── Identity facet splitting ────────────────────────────────────────


def test_identity_loads_with_h2_facet_split() -> None:
    """Crystal text with `## ` h2 headers populates the facets dict."""
    crystal_text = (
        "## Who I Am\n"
        "I am Fathom, a persistent agent.\n"
        "## What I Care About\n"
        "Practical solutions over theoretical ones.\n"
    )

    async def _fake_latest(force=False):
        return {
            "id": "abc123",
            "content": crystal_text,
            "timestamp": "2026-04-29T12:00:00Z",
        }

    with patch.object(sp_mod, "crystal_mod") as crystal_mock:
        crystal_mock.latest = _fake_latest
        out = asyncio.run(sp_mod._load_identity())
    assert out.text == crystal_text.strip()
    assert "Who I Am" in out.facets
    assert "What I Care About" in out.facets
    assert "I am Fathom" in out.facets["Who I Am"]
    assert out.delta_id == "abc123"


def test_identity_missing_crystal_yields_empty_object() -> None:
    """No crystal in lake → empty Identity, not None. Consumers can
    safely read .text / .facets without nil checks."""

    async def _fake_latest(force=False):
        return None

    with patch.object(sp_mod, "crystal_mod") as crystal_mock:
        crystal_mock.latest = _fake_latest
        out = asyncio.run(sp_mod._load_identity())
    assert isinstance(out, Identity)
    assert out.text == ""
    assert out.facets == {}
    assert out.delta_id is None


def test_identity_load_soft_fails_on_crystal_exception() -> None:
    """Crystal load throws → empty Identity, no propagation."""

    async def _fake_latest(force=False):
        raise RuntimeError("crystal store down")

    with patch.object(sp_mod, "crystal_mod") as crystal_mock:
        crystal_mock.latest = _fake_latest
        out = asyncio.run(sp_mod._load_identity())
    assert isinstance(out, Identity)
    assert out.text == ""


# ── Affect loading ──────────────────────────────────────────────────


def test_affect_loads_full_mood_payload() -> None:
    async def _fake_latest_mood():
        return {
            "state": "settled",
            "headline": "good rhythm",
            "subtext": "things are flowing",
            "carrier_wave": "steady focus, low noise floor",
            "threads": ["timeline-recall", "river-refactor"],
            "delta_id": "mood-1",
            "timestamp": "2026-04-29T12:30:00Z",
        }

    with patch.object(sp_mod, "mood_mod") as mood_mock:
        mood_mock.latest_mood = _fake_latest_mood
        out = asyncio.run(sp_mod._load_affect())
    assert out.state == "settled"
    assert out.headline == "good rhythm"
    assert out.threads == ["timeline-recall", "river-refactor"]
    assert out.delta_id == "mood-1"


def test_affect_missing_mood_yields_unset() -> None:
    async def _fake_latest_mood():
        return None

    with patch.object(sp_mod, "mood_mod") as mood_mock:
        mood_mock.latest_mood = _fake_latest_mood
        out = asyncio.run(sp_mod._load_affect())
    assert out.state == "unset"
    assert out.headline == ""


def test_affect_soft_fails_on_mood_exception() -> None:
    async def _fake_latest_mood():
        raise RuntimeError("mood module down")

    with patch.object(sp_mod, "mood_mod") as mood_mock:
        mood_mock.latest_mood = _fake_latest_mood
        out = asyncio.run(sp_mod._load_affect())
    assert out.state == "unset"


# ── Endorsement parsing ─────────────────────────────────────────────


def _engagement_delta(tags: list[str], content: str = "yep", ts: str = "2026-04-29T11:00:00Z") -> dict:
    return {
        "id": "x",
        "tags": tags,
        "content": content,
        "timestamp": ts,
    }


def test_endorsements_pick_up_affirms_refutes_from_tags() -> None:
    rows = [
        _engagement_delta(["affirms:abc12345"], "good take"),
        _engagement_delta(["refutes:def67890"], "no, not quite"),
        _engagement_delta(["from:ghi11111"], "synthesis cite"),
        _engagement_delta(["reply-to:jkl22222"]),
        _engagement_delta(["engages:mno33333"]),
        _engagement_delta(["unrelated:tag"]),  # ignored
        _engagement_delta([]),                  # ignored
    ]

    async def _fake_query(*args, **kwargs):
        return rows

    with patch.object(sp_mod.delta_client, "query", _fake_query):
        out = asyncio.run(sp_mod._load_endorsements())
    kinds = [e.kind for e in out]
    assert "affirms" in kinds
    assert "refutes" in kinds
    assert "from" in kinds
    assert "reply-to" in kinds
    assert "engages" in kinds
    # Six valid sources, last two filtered out.
    assert len(out) == 5


def test_endorsements_truncates_to_max() -> None:
    rows = [_engagement_delta([f"affirms:t{i:08d}"]) for i in range(100)]

    async def _fake_query(*args, **kwargs):
        return rows

    with patch.object(sp_mod.delta_client, "query", _fake_query):
        out = asyncio.run(sp_mod._load_endorsements())
    assert len(out) == sp_mod._MAX_ENDORSEMENTS


def test_endorsements_excerpt_truncates_long_content() -> None:
    rows = [_engagement_delta(["affirms:abc12345"], content="x" * 500)]

    async def _fake_query(*args, **kwargs):
        return rows

    with patch.object(sp_mod.delta_client, "query", _fake_query):
        out = asyncio.run(sp_mod._load_endorsements())
    assert len(out) == 1
    assert len(out[0].excerpt) <= 160


def test_endorsements_target_id_truncates_to_24_chars() -> None:
    rows = [
        _engagement_delta(
            [f"affirms:{'a' * 64}"],
        )
    ]

    async def _fake_query(*args, **kwargs):
        return rows

    with patch.object(sp_mod.delta_client, "query", _fake_query):
        out = asyncio.run(sp_mod._load_endorsements())
    assert len(out[0].target_id) == 24


def test_endorsements_soft_fails_on_query_exception() -> None:
    async def _fake_query(*args, **kwargs):
        raise RuntimeError("lake down")

    with patch.object(sp_mod.delta_client, "query", _fake_query):
        out = asyncio.run(sp_mod._load_endorsements())
    assert out == []


# ── Understanding (sediment) loading ────────────────────────────────


def test_understanding_extracts_from_provenance() -> None:
    rows = [
        {
            "id": "sed-1",
            "content": "I keep concluding that timeline-anchors-first is the right shape.",
            "tags": ["kind:sediment", "from:src11111", "from:src22222"],
            "timestamp": "2026-04-29T10:00:00Z",
        }
    ]

    async def _fake_query(*args, **kwargs):
        return rows

    with patch.object(sp_mod.delta_client, "query", _fake_query):
        out = asyncio.run(sp_mod._load_understanding())
    assert len(out) == 1
    assert out[0].delta_id == "sed-1"
    assert out[0].from_ids == ["src11111", "src22222"]


def test_understanding_soft_fails_on_query_exception() -> None:
    async def _fake_query(*args, **kwargs):
        raise RuntimeError("lake down")

    with patch.object(sp_mod.delta_client, "query", _fake_query):
        out = asyncio.run(sp_mod._load_understanding())
    assert out == []


# ── Standpoint integration ──────────────────────────────────────────


def test_current_gathers_all_components_in_one_object() -> None:
    """End-to-end: current() returns a Standpoint with every component
    populated from its corresponding loader, and posture inferred from
    identity+affect."""

    async def _fake_latest(force=False):
        return {"id": "c", "content": "## Self\nI am.", "timestamp": "t"}

    async def _fake_latest_mood():
        return {
            "state": "wired",
            "headline": "running hot",
            "subtext": "",
            "carrier_wave": "",
            "threads": [],
            "delta_id": "m",
            "timestamp": "t",
        }

    async def _fake_query(tags_include=None, **kwargs):
        if tags_include and "kind:sediment" in tags_include:
            return [
                {
                    "id": "s",
                    "content": "concluded.",
                    "tags": ["kind:sediment", "from:abc11111"],
                    "timestamp": "t",
                }
            ]
        return [_engagement_delta(["affirms:abc11111"])]

    with patch.object(sp_mod, "crystal_mod") as crystal_mock, patch.object(
        sp_mod, "mood_mod"
    ) as mood_mock, patch.object(sp_mod.delta_client, "query", _fake_query):
        crystal_mock.latest = _fake_latest
        mood_mock.latest_mood = _fake_latest_mood
        out = asyncio.run(sp_mod.current(session_tag="chat:test"))

    assert isinstance(out, Standpoint)
    assert out.identity.text.startswith("## Self")
    assert out.affect.state == "wired"
    assert out.posture == "fast"  # wired → fast
    assert len(out.endorsements) == 1
    assert len(out.understanding) == 1
    assert out.session_tag == "chat:test"
    assert out.captured_at  # ISO timestamp present


# ── Renderer ────────────────────────────────────────────────────────


def _full_standpoint() -> Standpoint:
    return Standpoint(
        identity=Identity(
            text="## Who I Am\nI am Fathom, persistent across compaction.",
            facets={"Who I Am": "I am Fathom, persistent across compaction."},
        ),
        affect=Affect(
            state="settled",
            headline="good rhythm",
            carrier_wave="steady focus",
        ),
        endorsements=[
            Endorsement(kind="affirms", target_id="abc1234567890123", excerpt="this matters"),
            Endorsement(kind="refutes", target_id="def4567890123456", excerpt="not that take"),
        ],
        understanding=[
            Sediment(
                delta_id="s1",
                content="Timeline anchors first is the right shape. Other things follow.",
                from_ids=["abc11111"],
            )
        ],
        posture="generous",
    )


def test_render_includes_posture_and_affect() -> None:
    out = render_for_prompt(_full_standpoint())
    assert "posture: generous" in out
    assert "settled" in out
    assert "good rhythm" in out


def test_render_includes_identity_snippet() -> None:
    out = render_for_prompt(_full_standpoint())
    assert "I am Fathom" in out


def test_render_includes_endorsements_and_understanding() -> None:
    out = render_for_prompt(_full_standpoint())
    assert "recently committed:" in out
    assert "affirms abc12345" in out
    assert "recently concluded:" in out
    assert "Timeline anchors first" in out


def test_render_respects_char_budget() -> None:
    """Tight budget truncates trailing sections; required header still
    fits."""
    out = render_for_prompt(_full_standpoint(), char_budget=80)
    assert len(out) <= 80
    assert "posture:" in out


def test_render_zero_budget_returns_empty() -> None:
    assert render_for_prompt(_full_standpoint(), char_budget=0) == ""


def test_render_skips_unset_affect() -> None:
    sp = Standpoint(
        identity=Identity(),
        affect=Affect(),  # unset
        endorsements=[],
        understanding=[],
        posture="neutral",
    )
    out = render_for_prompt(sp)
    assert "posture: neutral" in out
    assert "affect:" not in out
