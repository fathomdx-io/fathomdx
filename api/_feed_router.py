"""Router stage — maps (pass kind, judge axes) to a log level.

Pure function. No I/O. Called from feed_loop._produce_card / _produce_cards
after the judge stage. Returns one of:

  "ALERT"  — piercing tier; bypasses the default UI verbosity filter
  "NOTICE" — default user-facing tier
  "INFO"   — worth seeing if the user dials up the verbosity
  "DEBUG"  — reflections, mostly-provenance items
  "TRACE"  — weak-signal bridging, low-salience nostalgia
  None     — DROP; not worth writing to the lake at all

The level is monotonic — the UI filter "show level >= X" naturally
includes everything more important than the selected band.

Throwaway is a first-class destination. An empty-cycle outcome (every
candidate dropped) is healthy; the feed loop renders that as a
"great no-news day" state instead of pretending something landed.
"""

from __future__ import annotations

from .settings import settings as _default_settings

# Strict ordering. The UI dropdown and routes/feed.py filter both rely
# on this. Index = "importance"; ALERT (0) is most-important.
LEVEL_ORDER: tuple[str, ...] = ("ALERT", "NOTICE", "INFO", "DEBUG", "TRACE")


def level_rank(level: str) -> int:
    """Index of `level` in LEVEL_ORDER. Unknown levels sort to the back
    (treated as least-important) so a typo can never accidentally
    pierce the alert filter."""
    try:
        return LEVEL_ORDER.index(level)
    except ValueError:
        return len(LEVEL_ORDER)


def levels_at_or_above(min_level: str) -> list[str]:
    """All level names ≥ `min_level` in importance, in order from most
    to least important. Used by the route handler to expand a single
    'show NOTICE and above' query into the set of accepted level tags."""
    threshold = level_rank(min_level)
    return [lvl for lvl in LEVEL_ORDER if level_rank(lvl) <= threshold]


def route(
    kind: str,
    axes: dict[str, float],
    config=None,
) -> str | None:
    """Decide the level for a candidate card. None = DROP.

    Args:
      kind: the pass that produced the card. "alert"/"reflection"/
        "bridging"/"discrepancy"/"per_line"/"drift"/"volunteered".
      axes: the judge's output. Keys: salience, novelty, resonance,
        confidence, comfort. Each in [0.0, 1.0].
      config: settings object (for testability). Defaults to module
        settings.
    """
    s = config or _default_settings

    salience = axes.get("salience", 0.0)
    confidence = axes.get("confidence", 0.0)
    resonance = axes.get("resonance", 0.0)
    comfort = axes.get("comfort", 0.5)

    # Drop floor: salience or confidence below the floor → trash. The
    # judge marked it as either inconsequential or possibly confabulated;
    # the lake doesn't need that as sediment.
    if salience < s.feed_axis_floor_salience:
        return None
    if confidence < s.feed_axis_floor_confidence:
        return None

    # ALERT — piercing tier.
    #   1. The pass itself is the alert pass → trust the pass intent
    #      (still gated by floors above).
    #   2. The judge said "very salient AND uncomfortable" — the
    #      uncomfortable-truth gate that prevents pleasant flattery
    #      drift.
    if kind == "alert":
        return "ALERT"
    if (
        salience >= s.feed_level_alert_salience
        and comfort <= s.feed_level_alert_comfort_max
    ):
        return "ALERT"

    # NOTICE — default user-facing tier. Salient AND resonant AND
    # confident enough to surface without dialing up.
    if (
        salience >= s.feed_level_notice_salience
        and resonance >= s.feed_level_notice_resonance
    ):
        return "NOTICE"

    # INFO — moderate salience, even without high resonance. Visible
    # when the user dials the verbosity to INFO+.
    if salience >= s.feed_level_info_salience:
        return "INFO"

    # DEBUG — reflections by default. Provenance is dense and
    # structurally low-salience; it lives at DEBUG so the lake builds
    # up sediment without crowding the default surface.
    if kind == "reflection":
        return "DEBUG"

    # TRACE — bridging weak signals, low-salience volunteered/drift.
    # Above the drop floor (so it has *some* value) but quiet.
    return "TRACE"
