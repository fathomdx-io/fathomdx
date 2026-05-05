"""Tests for the four-section scaffold composition in api/routines.py.

The dashboard editor and the LLM `routines` tool both compose routine
prompts from four named sections (purpose / needs / steps / ending).
This module covers the server-side join logic that takes those fields
out of incoming bodies and emits the canonical `# Purpose / # Needs /
# Steps / # Ending` markdown.
"""

from __future__ import annotations

from api import routines


# ── _compose_scaffold_prompt ──────────────────────────────────────


def test_compose_scaffold_returns_none_when_no_scaffold_field_present() -> None:
    """A body that's just freeform `prompt` should pass through —
    composer returns None and the caller falls back to body['prompt']."""
    assert routines._compose_scaffold_prompt({"prompt": "freeform body"}) is None
    assert routines._compose_scaffold_prompt({"name": "x"}) is None


def test_compose_scaffold_emits_all_four_headers_even_with_empty_sections() -> None:
    """Empty sections still get headers — the next open detects scaffold
    shape via the four `# Section` markers, not the bodies under them."""
    out = routines._compose_scaffold_prompt({"purpose": "do a thing"})
    assert out is not None
    assert "# Purpose" in out
    assert "# Needs" in out
    assert "# Steps" in out
    assert "# Ending" in out
    assert "do a thing" in out


def test_compose_scaffold_packs_all_sections() -> None:
    out = routines._compose_scaffold_prompt({
        "purpose": "Track mailbox engagement.",
        "needs":   "claude-code on myras-fedora-laptop",
        "steps":   "1. Pull mailbox deltas.\n2. Score recency.",
        "ending":  "Send me a card with the result.",
    })
    assert "# Purpose\nTrack mailbox engagement." in out
    assert "# Needs\nclaude-code on myras-fedora-laptop" in out
    assert "# Steps\n1. Pull mailbox deltas.\n2. Score recency." in out
    assert "# Ending\nSend me a card with the result." in out


def test_compose_scaffold_strips_per_section_whitespace() -> None:
    """Leading/trailing whitespace in each field is normalized so the
    saved body has predictable shape."""
    out = routines._compose_scaffold_prompt({
        "purpose": "   p  ",
        "needs":   "\n\n n \n",
        "steps":   " s ",
        "ending":  " e ",
    })
    assert "# Purpose\np\n" in out
    assert "# Needs\nn\n" in out
    assert "# Steps\ns\n" in out
    assert "# Ending\ne\n" in out


def test_compose_scaffold_round_trips_through_parser_logic() -> None:
    """The composed body should be re-parseable by the same regex the
    dashboard editor uses (^#\\s*(Purpose|Needs|Steps|Ending)\\b in
    multiline mode). Verifies the headers land at line start with no
    leading whitespace."""
    import re
    out = routines._compose_scaffold_prompt({
        "purpose": "p", "needs": "n", "steps": "s", "ending": "e",
    })
    headers = re.findall(r"^#\s*(Purpose|Needs|Steps|Ending)\b", out, re.MULTILINE)
    assert headers == ["Purpose", "Needs", "Steps", "Ending"]


# ── _merge_meta uses scaffold when present, falls back to prompt ──


def test_merge_meta_prefers_scaffold_over_freeform_prompt() -> None:
    """When the body has BOTH `prompt` and scaffold fields, scaffold
    composition wins. The schema doc tells the LLM to use scaffold;
    the freeform `prompt` field is back-compat fallback only."""
    body = {
        "id": "x",
        "name": "X",
        "schedule": "0 * * * *",
        "purpose": "do x",
        "needs": "substrate",
        "steps": "step",
        "ending": "card",
        "prompt": "this should be ignored",
    }
    meta, prompt, _ws = routines._merge_meta(body)
    assert "# Purpose\ndo x" in prompt
    assert "this should be ignored" not in prompt


def test_merge_meta_uses_freeform_prompt_when_no_scaffold_fields() -> None:
    """A pure freeform body — back-compat path. Common for older
    callers and migration cases where the legacy prompt is passed
    through unchanged."""
    body = {
        "id": "x",
        "name": "X",
        "schedule": "0 * * * *",
        "prompt": "freeform body without sections",
    }
    meta, prompt, _ws = routines._merge_meta(body)
    assert prompt == "freeform body without sections"


def test_merge_meta_empty_body_returns_empty_prompt() -> None:
    """No scaffold fields, no prompt — empty body, empty prompt.
    Doesn't crash; downstream validation elsewhere checks required
    fields like id/name."""
    meta, prompt, _ws = routines._merge_meta({})
    assert prompt == ""


def test_merge_meta_partial_scaffold_still_composes() -> None:
    """Only `steps` provided — still composes a four-section body
    with empty Purpose/Needs/Ending headers. This mirrors what the
    editor saves for a freshly-opened legacy routine that's been
    slotted into Steps."""
    body = {
        "id": "x", "name": "X", "schedule": "0 * * * *",
        "steps": "this is the legacy prompt body",
    }
    meta, prompt, _ws = routines._merge_meta(body)
    assert "# Purpose\n\n" in prompt
    assert "# Steps\nthis is the legacy prompt body" in prompt
