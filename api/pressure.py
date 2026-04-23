"""Mood-layer pressure metric — derived from the lake.

Pressure answers: "how much salient activity has built up since the last
mood synthesis?" It's not stored. We compute it on read by asking the
lake for a weighted-and-decayed aggregate over deltas in the relevant
window.

Source weights and decay half-life live here (the mood-layer policy);
the aggregation itself is performed in SQL inside the delta-store so
every delta in the window contributes, not just the most recent N.

The only state we persist is the wake-control file (last_synthesis_at +
last_wake_at) — the reset and contrast-wake markers. Everything else is
a derived view, sibling to /v1/usage.
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
# Source weights — how much each delta source contributes to pressure.
SOURCE_WEIGHTS: dict[str, float] = {
    "fathom-chat": 1.5,
    "fathom-feed": 0.5,
    "fathom-mood": 0.0,  # mood deltas don't drive their own resynthesis
    "fathom-source-runner": 0.4,
    "fathom-agent": 0.2,
    "claude-code": 0.8,
    "consumer-api": 0.8,
}
USER_TAG_BOOST: float = 0.5
DEFAULT_WEIGHT: float = 0.3

# How far back to look when computing pressure. We never look further than
# this — older deltas have decayed to negligible weight anyway.
PRESSURE_WINDOW_HOURS: int = 36

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
    return Path(settings.mood_state_path)


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
        # Strip any legacy keys from the previous counter-based design.
        return {
            "last_wake_at": data.get("last_wake_at"),
            "last_synthesis_at": data.get("last_synthesis_at"),
        }
    except Exception:
        return _empty_state()


def _save_raw(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mood-state-", dir=str(p.parent))
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
    """Compute current pressure from the lake.

    Pressure = sum over deltas since last_synthesis_at of:
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
            half_life_seconds=settings.mood_decay_half_life_seconds,
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
        "threshold": settings.mood_pressure_threshold,
        "contrast_wake_seconds": settings.mood_contrast_wake_seconds,
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
    """Rolling pressure curve across the window — SQL-bucketed.

    For each of `buckets` ticks, the lake computes
        Σ w(d) × 0.5^((tick − ts(d)) / half_life)
    over every delta written after the most recent mood-synthesis
    anchor at-or-before that tick. No row-limit truncation.
    """
    window_seconds = since_seconds or PRESSURE_WINDOW_HOURS * 3600
    try:
        return await delta_client.pressure_history(
            since_seconds=window_seconds,
            buckets=buckets,
            weights=SOURCE_WEIGHTS,
            default_weight=DEFAULT_WEIGHT,
            user_tag_boost=USER_TAG_BOOST,
            half_life_seconds=settings.mood_decay_half_life_seconds,
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
