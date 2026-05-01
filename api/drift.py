"""Crystal drift snapshots — anchor-based.

Drift is cosine distance between the lake centroid right now and the
anchor centroid snapshotted at the last accepted crystal regen. This
decouples drift from the crystal's own text embedding, so a short or
failure-mode crystal can no longer self-trigger a runaway regen loop.

Each /v1/drift call samples the current centroid, compares to the
anchor, and appends a point to drift-history.json for the ECG widget.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from . import crystal as crystal_module
from . import crystal_anchor, delta_client
from ._time import now as _now
from .settings import settings

# Hard ceiling on history rows. The compactor below buckets older
# samples so a year of coverage fits well under this cap; the limit
# is just a runaway-growth safety net.
HISTORY_LIMIT: int = 50_000

# Tiered retention buckets — newest tier first. Each entry is
# ``(max_age_seconds, bucket_seconds)``: a sample whose age (vs. now)
# falls within ``max_age_seconds`` collapses to one row per
# ``bucket_seconds``. Older samples cascade into the next tier.
# Total bucket count for a year of coverage:
#   24h × 60/min  = 1440
#   6d  × 96/day  =  576   (15-min buckets)
#   23d × 24/day  =  552   (hourly buckets)
#   335d × 1/day  =  335   (daily buckets)
#                = ~2900 rows for a full year
_RETENTION_TIERS: tuple[tuple[int, int], ...] = (
    (24 * 3600,        60),         # last 24h: per-minute
    (7 * 24 * 3600,    15 * 60),    # 24h-7d:   per-15-min
    (30 * 24 * 3600,   3600),       # 7d-30d:   per-hour
    (365 * 24 * 3600,  86400),      # 30d-365d: per-day
)

_lock = asyncio.Lock()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _path() -> Path:
    """Place drift-history.json next to the mood-state file."""
    base = Path(settings.mood_state_path).parent
    return base / "drift-history.json"


def _load_raw() -> dict:
    p = _path()
    if not p.exists():
        return {"history": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"history": []}


def _compact_history(history: list[dict], now_ts: float) -> list[dict]:
    """Tier-bucket samples so a year of drift fits in ~3k rows.

    Walks newest→oldest. For each sample, picks the bucket size from
    ``_RETENTION_TIERS`` based on age, then keeps only the freshest
    sample per (tier, bucket-index). Samples older than the last tier
    drop entirely.
    """
    if not history:
        return history
    seen: set[tuple[int, int]] = set()
    kept: list[dict] = []
    for entry in sorted(history, key=lambda e: e.get("t") or "", reverse=True):
        try:
            ts_str = entry.get("t") or ""
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        age = max(0, now_ts - ts)
        tier_idx = -1
        bucket_secs = 0
        for i, (max_age, bsecs) in enumerate(_RETENTION_TIERS):
            if age <= max_age:
                tier_idx = i
                bucket_secs = bsecs
                break
        if tier_idx < 0:
            continue  # older than the longest tier — drop
        key = (tier_idx, int(ts // bucket_secs))
        if key in seen:
            continue
        seen.add(key)
        kept.append(entry)
    kept.sort(key=lambda e: e.get("t") or "")
    return kept


def _save_raw(state: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".drift-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


async def sample() -> dict:
    """Sample current drift against the anchor, append to history.

    Returns a snapshot dict with at least {drift, sampled_at}. Optional
    flags: no_crystal (no crystal ever generated), no_anchor (crystal
    exists but anchor file missing — usually a pre-anchor-era install
    or a corrupted sidecar), error (centroid fetch failed).
    """
    try:
        current = await crystal_module.latest()
    except Exception:
        # Lake unreachable — do NOT return no_crystal (would spuriously
        # trigger a bootstrap-fire in auto_regen on a transient hiccup).
        entry_t = _iso(_now())
        return {
            "drift": 0.0,
            "new_deltas": 0,
            "total_deltas": 0,
            "error": True,
            "sampled_at": entry_t,
        }
    anchor = await crystal_anchor.load()

    if not current or not current.get("text"):
        snapshot = {"drift": 0.0, "new_deltas": 0, "total_deltas": 0, "no_crystal": True}
    elif not anchor:
        # Crystal present but no anchor — don't signal drift (the
        # auto-regen poller reads no_anchor and skips, rather than
        # firing a bootstrap regen against a state that isn't actually
        # empty). Operator intervention or next accepted regen will
        # populate the anchor.
        snapshot = {"drift": 0.0, "new_deltas": 0, "total_deltas": 0, "no_anchor": True}
    else:
        try:
            c = await delta_client.centroid()
            vec = c.get("centroid")
            total = int(c.get("total_deltas") or 0)
            if not vec:
                snapshot = {
                    "drift": 0.0,
                    "new_deltas": 0,
                    "total_deltas": total,
                    "empty_lake": True,
                }
            else:
                d = crystal_anchor.cosine_distance(anchor["centroid"], vec)
                snapshot = {
                    "drift": round(d, 4),
                    "new_deltas": 0,
                    "total_deltas": total,
                }
        except Exception:
            snapshot = {"drift": 0.0, "new_deltas": 0, "total_deltas": 0, "error": True}

    now = _now()
    entry = {
        "t": _iso(now),
        "v": float(snapshot.get("drift", 0.0)),
        "new": int(snapshot.get("new_deltas", 0)),
        "total": int(snapshot.get("total_deltas", 0)),
    }
    async with _lock:
        state = _load_raw()
        history = state.get("history") or []
        history.append(entry)
        # Compact tier-by-tier so older samples don't bloat the file.
        # Cheap: O(n log n) over ~3k rows in steady state. Run on every
        # write so a burst of dashboard reloads can't push us past the
        # ceiling between compactions.
        history = _compact_history(history, now.timestamp())
        if len(history) > HISTORY_LIMIT:
            history = history[-HISTORY_LIMIT:]
        state["history"] = history
        _save_raw(state)

    return {**snapshot, "sampled_at": entry["t"]}


async def history(since_seconds: int | None = None) -> list[dict]:
    """Return drift history. Optionally filter to last N seconds."""
    async with _lock:
        state = _load_raw()
        items = list(state.get("history") or [])
    if since_seconds is None:
        return items
    cutoff = _now().timestamp() - since_seconds
    out: list[dict] = []
    for entry in items:
        try:
            ts = datetime.fromisoformat(entry["t"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ts.timestamp() >= cutoff:
            out.append(entry)
    return out
