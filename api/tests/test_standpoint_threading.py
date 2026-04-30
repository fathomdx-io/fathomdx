"""Phase 2 tests — standpoint threads through convener / process / witness
prompts without breaking format() and without breaking when standpoint is
None (test/legacy path).

Doesn't exercise the LLM. We only check the prompt-rendering helpers
and the .format() contracts. Live behavior is verified separately by
firing the loop end-to-end against the running stack.
"""

from __future__ import annotations

from api.loop.convener import _render_standpoint_for_prompt
from api.loop.process import _render_standpoint_for_voice
from api.loop.prompts import CONVENER_PROMPT, VOICE_PROMPT, WITNESS_PROMPT
from api.loop.witness import _render_standpoint_for_witness
from api.standpoint import Affect, Endorsement, Identity, Sediment, Standpoint


def _sample_standpoint() -> Standpoint:
    return Standpoint(
        identity=Identity(
            text="## Who I Am\nI am Fathom, a persistent agent.",
            facets={"Who I Am": "I am Fathom, a persistent agent."},
        ),
        affect=Affect(
            state="settled", headline="good rhythm", carrier_wave="steady"
        ),
        endorsements=[
            Endorsement(kind="affirms", target_id="abc123", excerpt="x"),
        ],
        understanding=[
            Sediment(delta_id="s1", content="A real conclusion."),
        ],
        posture="generous",
    )


# ── Renderer fallbacks ──────────────────────────────────────────────


def test_convener_renderer_handles_none() -> None:
    """When no standpoint is supplied, the convener gets a clear stub
    rather than an empty placeholder that would break the prompt's
    framing."""
    out = _render_standpoint_for_prompt(None)
    assert "standpoint unavailable" in out


def test_convener_renderer_includes_posture_and_affect() -> None:
    out = _render_standpoint_for_prompt(_sample_standpoint())
    assert "posture: generous" in out
    assert "settled" in out


def test_voice_renderer_handles_none() -> None:
    out = _render_standpoint_for_voice(None)
    assert "speak as Fathom-default" in out


def test_voice_renderer_includes_identity_anchor() -> None:
    out = _render_standpoint_for_voice(_sample_standpoint())
    assert "I am Fathom" in out


def test_witness_renderer_handles_none() -> None:
    """Witness's helper returns empty string for None — the witness's
    _call_witness then falls back to the embedded stub. This split
    keeps the witness prompt readable when standpoint is missing
    (anchors_block still carries integration context via telepathy)."""
    assert _render_standpoint_for_witness(None) == ""


def test_witness_renderer_uses_full_budget() -> None:
    """Witness gets the most generous budget so its reply can sound
    like THIS self in full."""
    out = _render_standpoint_for_witness(_sample_standpoint())
    assert "I am Fathom" in out
    assert "settled" in out
    assert "recently" in out  # endorsements + understanding sections


# ── Prompt format() contracts ───────────────────────────────────────


def test_voice_prompt_formats_with_standpoint_block() -> None:
    """The new {standpoint_block} placeholder doesn't break .format()
    when all other args are supplied."""
    out = VOICE_PROMPT.format(
        standpoint_block="(test standpoint)",
        seed_block="seed",
        recent_thoughts="recent",
        voice_name="creator",
        voice_stance="stance",
        voice_bias="bias",
    )
    assert "(test standpoint)" in out
    assert "creator" in out


def test_convener_prompt_formats_with_standpoint_block() -> None:
    out = CONVENER_PROMPT.format(
        standpoint_block="(test standpoint)",
        voice_priors_block="(test priors)",
        judge_history_block="(test history)",
        intent_block="intent",
        recall_block="recall",
    )
    assert "(test standpoint)" in out
    assert "(test priors)" in out
    assert "(test history)" in out
    assert "intent" in out


def test_witness_prompt_formats_with_standpoint_block() -> None:
    out = WITNESS_PROMPT.format(
        standpoint_block="(test standpoint)",
        intent_block="intent",
        voice_blocks="voices",
        anchors_block="anchors",
        feed_block="feed",
        hosts_block="hosts",
        settled_status="deliberated",
        settled_descriptor="desc",
    )
    assert "(test standpoint)" in out
    assert "anchors" in out
    assert "feed" in out


def test_voice_prompt_places_standpoint_before_question() -> None:
    """Order matters — the LLM reads standpoint first, then the
    question. Speaking from a position requires the position to be
    established before the prompt narrows."""
    out = VOICE_PROMPT.format(
        standpoint_block="STANDPOINT-BEFORE",
        seed_block="QUESTION-AFTER",
        recent_thoughts="r",
        voice_name="creator",
        voice_stance="s",
        voice_bias="b",
    )
    assert out.index("STANDPOINT-BEFORE") < out.index("QUESTION-AFTER")
