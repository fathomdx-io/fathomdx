"""Phase 3 tests — witness fire as constituting act.

Pins parsing of the four self-state JSON fields and the lake writes
that emit attestation / mood-shift / engagement deltas in one act.
Each parser graceful-fails to its empty/none shape so an LLM that
forgets a field doesn't break the loop.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from api.loop.witness import (
    _clean_id_list,
    _parse_mood_shift,
    _write_constituting_writes,
)


# ── _parse_mood_shift ──────────────────────────────────────────────


def test_mood_shift_full_payload() -> None:
    raw = {"direction": "+", "axis": "settled", "magnitude": 0.15, "reason": "good fire"}
    out = _parse_mood_shift(raw)
    assert out == {
        "direction": "+",
        "axis": "settled",
        "magnitude": 0.15,
        "reason": "good fire",
    }


def test_mood_shift_clamps_magnitude_above_1() -> None:
    raw = {"direction": "+", "axis": "settled", "magnitude": 5.0, "reason": "x"}
    out = _parse_mood_shift(raw)
    assert out is not None
    assert out["magnitude"] == 1.0


def test_mood_shift_clamps_magnitude_below_0() -> None:
    raw = {"direction": "-", "axis": "tired", "magnitude": -0.5, "reason": "x"}
    out = _parse_mood_shift(raw)
    # Negative clamps to 0 → returns None (zero magnitude is "no shift").
    assert out is None


def test_mood_shift_zero_magnitude_returns_none() -> None:
    raw = {"direction": "+", "axis": "settled", "magnitude": 0.0, "reason": "x"}
    assert _parse_mood_shift(raw) is None


def test_mood_shift_invalid_direction_returns_none() -> None:
    raw = {"direction": "?", "axis": "settled", "magnitude": 0.1, "reason": "x"}
    assert _parse_mood_shift(raw) is None


def test_mood_shift_missing_axis_returns_none() -> None:
    raw = {"direction": "+", "axis": "", "magnitude": 0.1}
    assert _parse_mood_shift(raw) is None


def test_mood_shift_non_dict_returns_none() -> None:
    assert _parse_mood_shift(None) is None
    assert _parse_mood_shift("not a dict") is None
    assert _parse_mood_shift([]) is None


def test_mood_shift_empty_dict_returns_none() -> None:
    assert _parse_mood_shift({}) is None


def test_mood_shift_truncates_axis_and_reason() -> None:
    raw = {
        "direction": "+",
        "axis": "x" * 100,
        "magnitude": 0.1,
        "reason": "y" * 500,
    }
    out = _parse_mood_shift(raw)
    assert out is not None
    assert len(out["axis"]) <= 32
    assert len(out["reason"]) <= 160


# ── _clean_id_list ─────────────────────────────────────────────────


def test_clean_id_list_truncates_to_24_chars() -> None:
    out = _clean_id_list(["a" * 64, "b" * 32])
    assert len(out) == 2
    assert all(len(x) == 24 for x in out)


def test_clean_id_list_dedupes_preserving_order() -> None:
    out = _clean_id_list(["abc12345", "def67890", "abc12345", "ghi11111"])
    assert out == ["abc12345", "def67890", "ghi11111"]


def test_clean_id_list_drops_non_strings() -> None:
    out = _clean_id_list(["abc12345", None, 42, {"id": "x"}, "def67890"])
    assert out == ["abc12345", "def67890"]


def test_clean_id_list_drops_empty_strings() -> None:
    out = _clean_id_list(["abc12345", "", "  ", "def67890"])
    assert out == ["abc12345", "def67890"]


def test_clean_id_list_non_list_returns_empty() -> None:
    assert _clean_id_list(None) == []
    assert _clean_id_list("not a list") == []
    assert _clean_id_list({"id": "abc12345"}) == []


# ── _write_constituting_writes ─────────────────────────────────────


def _capture_writes() -> tuple[AsyncMock, list[dict]]:
    """Returns (mock_write, captured_calls). Each call is a dict with
    content/tags/source pulled from kwargs."""
    captured: list[dict] = []

    async def fake_write(**kwargs):
        captured.append(kwargs)
        return {"id": "lake-id-stub"}

    mock = AsyncMock(side_effect=fake_write)
    return mock, captured


def test_constituting_writes_attestation() -> None:
    mock_write, captured = _capture_writes()
    with patch("api.loop.witness.delta_client.write", mock_write):
        asyncio.run(
            _write_constituting_writes(
                lake_card_id="cardabc123def456789012",
                attestation="I learned that I default to terse under affect=tired.",
                mood_shift=None,
                cited_ids=[],
                dropped_ids=[],
            )
        )
    assert len(captured) == 1
    call = captured[0]
    assert call["content"].startswith("I learned")
    tags = call["tags"]
    assert "kind:standpoint-attestation" in tags
    assert any(t.startswith("from:cardabc123def456789012") for t in tags)
    assert call["source"] == "fathom-self"


def test_constituting_writes_skips_empty_attestation() -> None:
    mock_write, captured = _capture_writes()
    with patch("api.loop.witness.delta_client.write", mock_write):
        asyncio.run(
            _write_constituting_writes(
                lake_card_id="card123",
                attestation="",  # empty
                mood_shift=None,
                cited_ids=[],
                dropped_ids=[],
            )
        )
    assert captured == []


def test_constituting_writes_mood_shift() -> None:
    mock_write, captured = _capture_writes()
    shift = {"direction": "+", "axis": "settled", "magnitude": 0.1, "reason": "fit"}
    with patch("api.loop.witness.delta_client.write", mock_write):
        asyncio.run(
            _write_constituting_writes(
                lake_card_id="card123",
                attestation="",
                mood_shift=shift,
                cited_ids=[],
                dropped_ids=[],
            )
        )
    assert len(captured) == 1
    tags = captured[0]["tags"]
    assert "kind:mood-shift" in tags
    assert "mood-axis:settled" in tags
    assert "mood-direction:+" in tags
    # Content is the JSON of the shift
    assert '"axis": "settled"' in captured[0]["content"]


def test_constituting_writes_cited_ids_become_affirms() -> None:
    mock_write, captured = _capture_writes()
    with patch("api.loop.witness.delta_client.write", mock_write):
        asyncio.run(
            _write_constituting_writes(
                lake_card_id="card123",
                attestation="",
                mood_shift=None,
                cited_ids=["cited1abc12345678901234", "cited2def56789012345678"],
                dropped_ids=[],
            )
        )
    assert len(captured) == 2
    for call in captured:
        tags = call["tags"]
        assert "kind:engagement-attest" in tags
        assert any(t.startswith("affirms:cited") for t in tags)
        assert any(t.startswith("from:card123") for t in tags)


def test_constituting_writes_dropped_ids_become_refutes() -> None:
    mock_write, captured = _capture_writes()
    with patch("api.loop.witness.delta_client.write", mock_write):
        asyncio.run(
            _write_constituting_writes(
                lake_card_id="card123",
                attestation="",
                mood_shift=None,
                cited_ids=[],
                dropped_ids=["drop1abcdef12345678abcd"],
            )
        )
    assert len(captured) == 1
    tags = captured[0]["tags"]
    assert "kind:engagement-attest" in tags
    assert any(t.startswith("refutes:drop1abcdef") for t in tags)


def test_constituting_writes_full_act_writes_all_four() -> None:
    """A fire that produced all four components writes 1 attestation +
    1 mood-shift + N engagement deltas, in one constituting act."""
    mock_write, captured = _capture_writes()
    with patch("api.loop.witness.delta_client.write", mock_write):
        asyncio.run(
            _write_constituting_writes(
                lake_card_id="cardABCDEF123456789012",
                attestation="I sat with the question instead of fixing.",
                mood_shift={"direction": "-", "axis": "wired", "magnitude": 0.1, "reason": "settled"},
                cited_ids=["cite1abc12345678901234"],
                dropped_ids=["drop1abc12345678901234"],
            )
        )
    # 1 attestation + 1 mood + 1 cited + 1 dropped = 4
    assert len(captured) == 4
    kinds_emitted = []
    for c in captured:
        for t in c["tags"]:
            if t.startswith("kind:"):
                kinds_emitted.append(t)
                break
    assert "kind:standpoint-attestation" in kinds_emitted
    assert "kind:mood-shift" in kinds_emitted
    assert kinds_emitted.count("kind:engagement-attest") == 2


def test_constituting_writes_no_writes_when_all_empty() -> None:
    mock_write, captured = _capture_writes()
    with patch("api.loop.witness.delta_client.write", mock_write):
        asyncio.run(
            _write_constituting_writes(
                lake_card_id="card123",
                attestation="",
                mood_shift=None,
                cited_ids=[],
                dropped_ids=[],
            )
        )
    assert captured == []
