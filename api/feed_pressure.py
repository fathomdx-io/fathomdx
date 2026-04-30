"""Feed-layer pressure metric — derived from the lake.

Sibling of api/pressure.py. Same primitive (weighted-and-decayed lake
volume since last synthesis), tuned for feed regeneration rather than
mood synthesis. The Stats legend at ui/index.html names the shared
intuition: pressure is "what's been building up since the last
check-in. That feeling when there's too much you haven't sat with yet."
Synthesis is the act of sitting with it. Mood synthesis sits with
affective state; feed synthesis sits with content the user hasn't
encountered yet.

Two differences from mood pressure worth naming:

1. Weights tilt toward content surfaces. RSS/source-runner doubles
   from mood's weight because the feed is where external content
   lands. claude-code nudges up because feed cards reflect visible
   work, not just affective load.

2. fathom-engagement is a heavily-weighted source. By the third law
   of "what sticks and what wilts," an `affirms`/`refutes`/`reply-to`
   delta promotes its target into authored memory. We get that effect
   numerically, free, by upweighting the engagement deltas themselves
   in the source-weight dict — no per-delta lookup needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from . import delta_client
from ._time import now as _now
from .settings import settings

# ── Weights ─────────────────────────────────────
# Source weights — tuned for "what's worth carding," not "what shifts
# mood." Cross-reference api/pressure.py:33-43 — most values diverge
# deliberately.
SOURCE_WEIGHTS: dict[str, float] = {
    "fathom-chat": 1.5,
    "claude-code": 1.0,
    "fathom-source-runner": 0.8,  # doubled vs mood — feed is where RSS lands
    "fathom-engagement": 1.5,     # third-law promotion, in numerical form
                                  # (also covers misc consumer-api writes —
                                  # alerts, receipts, crystal regens — after
                                  # the consumer-api → fathom-engagement
                                  # rename collapsed them into one source)
    "fathom-agent": 0.1,
    "fathom-feed": 0.0,           # exclude — would feedback-loop on its own output
    "fathom-mood": 0.0,           # exclude — mood is its own synthesis stream
    "witness": 0.0,               # exclude — witness cards are this loop's own
                                  # output; counting them as activity is what
                                  # makes pressure tip on its own writes and
                                  # re-fire pulse passes about the loop's own
                                  # recent outputs (compounded by multi-card)
    "fathom-self": 0.0,           # exclude — attestations, mood-shifts, voice
                                  # affirmations, engagement-attests; same
                                  # self-amplification as witness
    "fathom-sediment": 0.0,       # exclude — auto-sediment from deep recall;
                                  # high-volume self-output, same pattern
    "fathom-loop": 0.0,           # exclude — loop's own thinking-aloud writes
    "judge": 0.0,                 # exclude — kind:judge-axes side-channel
}
USER_TAG_BOOST: float = 0.5
DEFAULT_WEIGHT: float = 0.3

# How far back the SQL aggregation looks. Tighter than mood's 36h —
# feed cards are "what's been happening today," not a multi-day arc.
PRESSURE_WINDOW_HOURS: int = 24

# ── Persisted state (the small bit) ─────────────
_lock = asyncio.Lock()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _state_path() -> Path:
    return Path(settings.feed_pressure_state_path)


def _empty_state() -> dict:
    return {
        "last_wake_at": None,
        "last_synthesis_at": None,
    }


def _load_raw() -> dict:
    p = _state_path()
    if not p.exists():
        return _empty_state()
    try:
        data = json.loads(p.read_text())
        return {
            "last_wake_at": data.get("last_wake_at"),
            "last_synthesis_at": data.get("last_synthesis_at"),
        }
    except Exception:
        return _empty_state()


def _save_raw(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".feed-pressure-state-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


# ── Public: read ────────────────────────────────


async def read_pressure() -> dict:
    """Compute current feed pressure from the lake.

    Volume = sum over deltas since last_synthesis_at of:
                 weight(source, tags) × decay(now - delta_ts)

    If no synthesis has ever fired, we use the full window.
    """
    state = _load_raw()
    last_synth = _parse(state.get("last_synthesis_at"))
    last_wake = _parse(state.get("last_wake_at"))
    now = _now()

    try:
        volume = await delta_client.pressure_volume(
            cutoff_ts=_iso(last_synth) if last_synth else None,
            window_seconds=PRESSURE_WINDOW_HOURS * 3600,
            weights=SOURCE_WEIGHTS,
            default_weight=DEFAULT_WEIGHT,
            user_tag_boost=USER_TAG_BOOST,
            half_life_seconds=settings.feed_pressure_decay_half_life_seconds,
        )
    except Exception:
        volume = 0.0

    time_since_wake = (now - last_wake).total_seconds() if last_wake else None
    time_since_synth = (now - last_synth).total_seconds() if last_synth else None
    return {
        "volume": volume,
        "last_wake_at": last_wake,
        "last_synthesis_at": last_synth,
        "time_since_wake_seconds": time_since_wake,
        "time_since_synthesis_seconds": time_since_synth,
        "threshold": settings.feed_pressure_threshold,
        "contrast_wake_seconds": settings.feed_pressure_contrast_wake_seconds,
    }


async def should_synthesize() -> tuple[bool, str]:
    p = await read_pressure()
    if p["volume"] >= p["threshold"]:
        return True, "pressure"
    if p["last_synthesis_at"] is None:
        return True, "first-run"
    if (
        p["time_since_wake_seconds"] is not None
        and p["time_since_wake_seconds"] >= p["contrast_wake_seconds"]
    ):
        return True, "contrast-wake"
    return False, "below-threshold"


async def history(since_seconds: int | None = None, buckets: int = 60) -> list[dict]:
    """Rolling feed-pressure curve. Mirror of api/pressure.history.

    For each tick, the lake computes
        Σ w(d) × 0.5^((tick − ts(d)) / half_life)
    over every delta written after the most recent feed synthesis
    anchor at-or-before that tick.
    """
    window_seconds = since_seconds or PRESSURE_WINDOW_HOURS * 3600
    try:
        return await delta_client.pressure_history(
            since_seconds=window_seconds,
            buckets=buckets,
            weights=SOURCE_WEIGHTS,
            default_weight=DEFAULT_WEIGHT,
            user_tag_boost=USER_TAG_BOOST,
            half_life_seconds=settings.feed_pressure_decay_half_life_seconds,
        )
    except Exception:
        return []


# ── Public: write (just the markers) ────────────


async def mark_wake() -> None:
    async with _lock:
        state = _load_raw()
        state["last_wake_at"] = _iso(_now())
        _save_raw(state)


async def mark_synthesis() -> None:
    async with _lock:
        state = _load_raw()
        state["last_synthesis_at"] = _iso(_now())
        _save_raw(state)
