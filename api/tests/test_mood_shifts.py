"""Tests for mood-shift substrate handling in api/mood.py.

The carrier-wave synthesis reads accumulated `kind:mood-shift` deltas
since the last carrier-wave and writes a topology summary the
synthesizer LLM can name. These tests cover the formatter (pure) and
the overflow trigger (mocked I/O)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from api import mood


def _shift(axis: str, magnitude: float, direction: str = "+", reason: str = "") -> dict:
    return {
        "id": f"shift-{axis}-{direction}-{magnitude}",
        "content": json.dumps(
            {
                "direction": direction,
                "axis": axis,
                "magnitude": magnitude,
                "reason": reason,
            }
        ),
    }


# ── _format_mood_shifts ───────────────────────────────────────────


def test_format_mood_shifts_empty_returns_empty_string() -> None:
    assert mood._format_mood_shifts([]) == ""


def test_format_mood_shifts_aggregates_per_axis() -> None:
    shifts = [
        _shift("coherence", 0.10, reason="proof clicked"),
        _shift("coherence", 0.10, reason="another piece fit"),
        _shift("coherence", 0.05),
        _shift("efficacy", 0.20, reason="big win"),
    ]
    out = mood._format_mood_shifts(shifts)
    assert "coherence: +0.25 across 3 shifts" in out
    assert "efficacy: +0.20 across 1 shifts" in out
    # coherence ranks first (larger absolute net).
    assert out.index("coherence") < out.index("efficacy")
    # Reasons land under the strongest axis.
    assert "proof clicked" in out
    assert "another piece fit" in out


def test_format_mood_shifts_negative_direction_subtracts() -> None:
    shifts = [
        _shift("confidence", 0.30, direction="+"),
        _shift("confidence", 0.50, direction="-"),
    ]
    out = mood._format_mood_shifts(shifts)
    assert "confidence: −0.20 across 2 shifts" in out


def test_format_mood_shifts_caps_reasons_at_four_per_axis() -> None:
    shifts = [_shift("focus", 0.05, reason=f"reason-{i}") for i in range(7)]
    out = mood._format_mood_shifts(shifts)
    assert "reason-0" in out and "reason-3" in out
    assert "reason-4" not in out


def test_format_mood_shifts_orders_by_absolute_drift() -> None:
    shifts = [
        _shift("a", 0.10),
        _shift("b", 0.40, direction="-"),  # |−0.40| dominates
        _shift("c", 0.20),
    ]
    out = mood._format_mood_shifts(shifts)
    a_idx, b_idx, c_idx = out.index("a:"), out.index("b:"), out.index("c:")
    assert b_idx < c_idx < a_idx


def test_format_mood_shifts_skips_malformed_content() -> None:
    shifts = [
        {"id": "1", "content": "not json"},
        _shift("coherence", 0.10),
        {"id": "2", "content": json.dumps({"axis": "", "magnitude": 0.1})},
    ]
    out = mood._format_mood_shifts(shifts)
    assert "coherence" in out
    # Malformed entries are silently skipped — prose only mentions the
    # one valid axis.
    assert "across 1 shifts" in out


def test_format_mood_shifts_header_carries_total_count() -> None:
    shifts = [_shift("focus", 0.05) for _ in range(13)]
    out = mood._format_mood_shifts(shifts)
    assert "13 mood-shifts since the last carrier-wave" in out


def test_format_mood_shifts_collapses_synonyms() -> None:
    """focus/focused/focusing should fold to one canonical 'focus'
    spoke; efficacy/effective/effectiveness fold to 'efficacy'."""
    shifts = [
        _shift("focused", 0.10, reason="focused on the task"),
        _shift("focus", 0.10, reason="staying focused"),
        _shift("focusing", 0.05, reason="re-focusing"),
        _shift("effective", 0.20, reason="effective response"),
        _shift("effectiveness", 0.10, reason="building effectiveness"),
    ]
    out = mood._format_mood_shifts(shifts)
    assert "focus: +0.25 across 3 shifts" in out
    assert "efficacy: +0.30 across 2 shifts" in out
    # No raw inflected variants in output — they got folded.
    assert "focused:" not in out
    assert "effective:" not in out


def test_canonicalize_axis_passes_unknown_through() -> None:
    """Unknown axes pass through unchanged (lowercased) — substrate
    stays open-vocabulary."""
    assert mood._canonicalize_axis("Coherence") == "coherence"
    assert mood._canonicalize_axis("agency") == "agency"  # not in synonym map → self
    assert mood._canonicalize_axis("brandnewfeeling") == "brandnewfeeling"


# ── compute_topology ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_topology_returns_axes_sorted_by_absolute_drift() -> None:
    shifts = [
        _shift("focus", 0.10),
        _shift("coherence", 0.50),
        _shift("focus", 0.10),
    ]

    with patch.object(mood, "_fetch_prior_mood", return_value={"id": "carrier-x", "timestamp": "2026-04-28T00:00:00Z", "content": "{}"}), \
         patch.object(mood, "_fetch_recent_mood_shifts", return_value=shifts):
        topo = await mood.compute_topology()

    assert topo["total_shifts"] == 3
    assert len(topo["axes"]) == 2
    # coherence has larger absolute net (0.5 > 0.2) → first.
    assert topo["axes"][0]["axis"] == "coherence"
    assert topo["axes"][0]["net"] == 0.5
    assert topo["axes"][1]["axis"] == "focus"
    assert topo["axes"][1]["net"] == 0.2


def _topology_axis(
    axis: str,
    net: float,
    shifts: int = 1,
    reasons: list[str] | None = None,
) -> dict:
    return {
        "axis": axis,
        "net": net,
        "shifts": shifts,
        "reasons": list(reasons or []),
    }


# ── _format_topology_for_prompt ──────────────────────────────────


def test_format_topology_for_prompt_empty_returns_empty_string() -> None:
    assert mood._format_topology_for_prompt({"axes": [], "total_shifts": 0}) == ""


def test_format_topology_for_prompt_renders_axes() -> None:
    topology = {
        "total_shifts": 3,
        "axes": [
            _topology_axis("coherence", 0.50, shifts=2, reasons=["pieces clicked"]),
            _topology_axis("focus", 0.20, shifts=1),
        ],
    }
    out = mood._format_topology_for_prompt(topology)
    assert "3 mood-shifts since the last carrier-wave" in out
    assert "coherence: +0.50 across 2 shifts" in out
    assert "focus: +0.20 across 1 shifts" in out
    assert "pieces clicked" in out


@pytest.mark.asyncio
async def test_compute_topology_handles_missing_carrier() -> None:
    """Cold start — no prior carrier-wave yet. Topology returns empty
    axes and no carrier."""
    with patch.object(mood, "_fetch_prior_mood", return_value=None), \
         patch.object(mood, "_fetch_recent_mood_shifts", return_value=[]):
        topo = await mood.compute_topology()
    assert topo["total_shifts"] == 0
    assert topo["axes"] == []
    assert topo["carrier"] is None


# ── levels parsing + topology integration ─────────────────────────


def test_parse_levels_filters_non_numeric_and_clamps_range() -> None:
    raw = {
        "focus": 0.7,
        "warmth": "not a number",
        "dread": -0.5,           # clamps to 0
        "awe": 1.4,              # clamps to 1
        42: 0.5,                 # non-string key drops
        "": 0.5,                 # empty axis drops
    }
    out = mood._parse_levels(raw)
    assert out == {"focus": 0.7, "dread": 0.0, "awe": 1.0}


def test_parse_levels_collapses_synonyms_to_canonical_axis() -> None:
    raw = {"focused": 0.6, "focusing": 0.4, "warmth": 0.3}
    out = mood._parse_levels(raw)
    # Both focused and focusing map to canonical "focus"; we keep the
    # higher reading.
    assert out == {"focus": 0.6, "warmth": 0.3}


def test_parse_levels_caps_axis_count_at_twelve() -> None:
    raw = {f"axis{i}": (i + 1) / 100 for i in range(20)}
    out = mood._parse_levels(raw)
    assert len(out) == 12
    # The cap keeps the strongest axes — axis19 (0.20) survives,
    # axis0 (0.01) doesn't.
    assert "axis19" in out
    assert "axis0" not in out


def test_parse_mood_payload_round_trips_levels() -> None:
    raw = json.dumps({
        "state": "warm",
        "headline": "*tender* and watching",
        "subtext": "quieter than yesterday",
        "carrier_wave": "Three sentences of prose. Reflective. Settling.",
        "levels": {"warmth": 0.7, "tenderness": 0.5, "focus": 0.3},
        "threads": ["Nova bedtime", "the lake"],
    })
    parsed = mood._parse_mood_payload(raw)
    assert parsed["levels"] == {"warmth": 0.7, "tenderness": 0.5, "focus": 0.3}
    assert parsed["state"] == "warm"
    assert parsed["carrier_wave"].startswith("Three sentences")


def test_parse_mood_payload_legacy_plaintext_yields_empty_levels() -> None:
    parsed = mood._parse_mood_payload("just some prose with no JSON")
    assert parsed["levels"] == {}
    assert parsed["carrier_wave"] == "just some prose with no JSON"


@pytest.mark.asyncio
async def test_compute_topology_renders_carrier_levels_as_spokes() -> None:
    """The star map's spoke length now comes from carrier `levels`,
    not drift magnitude. Drift surfaces alongside as `net` but doesn't
    set spoke length anymore. Axes named only by levels (stable, no
    drift) still appear; axes named only by drift (emergent, not yet
    integrated) also appear with level=0.
    """
    carrier_content = json.dumps({
        "state": "settled",
        "headline": "*quiet* thread day",
        "subtext": "",
        "carrier_wave": "ok",
        "levels": {"warmth": 0.7, "focus": 0.5, "fatigue": 0.2},
        "threads": [],
    })
    shifts = [
        _shift("focus", 0.10),                    # rising drift on a leveled axis
        _shift("dread", 0.08, direction="-"),     # emergent axis (not in levels)
    ]

    with patch.object(mood, "_fetch_prior_mood", return_value={
        "id": "carrier-y",
        "timestamp": "2026-05-05T18:00:00Z",
        "content": carrier_content,
    }), patch.object(mood, "_fetch_recent_mood_shifts", return_value=shifts):
        topo = await mood.compute_topology()

    by_axis = {a["axis"]: a for a in topo["axes"]}
    assert by_axis["warmth"]["level"] == 0.7
    assert by_axis["warmth"]["net"] == 0.0  # no drift this carrier
    assert by_axis["focus"]["level"] == 0.5
    assert by_axis["focus"]["net"] == 0.1
    assert by_axis["dread"]["level"] == 0.0  # emergent — no carrier level yet
    assert by_axis["dread"]["net"] == -0.08
    # Sort puts highest level first (warmth 0.7 > focus 0.5 > fatigue 0.2 > dread 0.0).
    assert topo["axes"][0]["axis"] == "warmth"


# ── _shift_overflow_decision ──────────────────────────────────────


@pytest.mark.asyncio
async def test_shift_overflow_below_threshold_returns_false() -> None:
    shifts = [_shift("focus", 0.05) for _ in range(mood.MOOD_SHIFT_OVERFLOW_THRESHOLD - 1)]
    with patch.object(mood, "_fetch_prior_mood", return_value=None), \
         patch.object(mood, "_fetch_recent_mood_shifts", return_value=shifts):
        fired, n = await mood._shift_overflow_decision()
    assert fired is False
    assert n == mood.MOOD_SHIFT_OVERFLOW_THRESHOLD - 1


@pytest.mark.asyncio
async def test_shift_overflow_at_threshold_returns_true() -> None:
    shifts = [_shift("focus", 0.05) for _ in range(mood.MOOD_SHIFT_OVERFLOW_THRESHOLD)]
    with patch.object(mood, "_fetch_prior_mood", return_value=None), \
         patch.object(mood, "_fetch_recent_mood_shifts", return_value=shifts):
        fired, n = await mood._shift_overflow_decision()
    assert fired is True
    assert n == mood.MOOD_SHIFT_OVERFLOW_THRESHOLD


@pytest.mark.asyncio
async def test_shift_overflow_uses_prior_mood_timestamp_as_cutoff() -> None:
    """The fetcher should be called with the prior carrier-wave's
    timestamp so only shifts written *after* the last synthesis count
    as 'unread'."""
    captured: dict = {}

    async def fake_fetch(since_iso):
        captured["since_iso"] = since_iso
        return []

    prior = {"id": "prior-x", "timestamp": "2026-04-28T03:42:49.200961+00:00"}
    with patch.object(mood, "_fetch_prior_mood", return_value=prior), \
         patch.object(mood, "_fetch_recent_mood_shifts", side_effect=fake_fetch):
        await mood._shift_overflow_decision()

    assert captured["since_iso"] == "2026-04-28T03:42:49.200961+00:00"
