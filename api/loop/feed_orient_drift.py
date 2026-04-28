"""Engagement-drift snapshots — anchor-based.

Engagement drift is the cosine distance between the current
feed-engagement centroid and the anchor centroid snapshotted at the
last accepted feed-orient regen. Same pattern as identity-crystal
drift (api/drift.py), but scoped to feed-engagement deltas.

Each call to `sample()` queries the lake's centroid filtered by
`feed-engagement` tag, compares to the anchor, and appends a point to
feed-drift-history.json for the dashboard's feed-orient ECG card.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .. import crystal_anchor, delta_client
from .._time import now as _now
from ..settings import settings
from . import feed_orient_anchor

HISTORY_LIMIT: int = 1000

_lock = asyncio.Lock()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _path() -> Path:
    base = Path(settings.mood_state_path).parent
    return base / "feed-drift-history.json"


def _load_raw() -> dict:
    p = _path()
    if not p.exists():
        return {"history": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"history": []}


def _save_raw(state: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".feed-drift-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


async def sample() -> dict:
    """Sample current engagement-drift against the anchor. Append to
    history. Returns the snapshot dict."""
    anchor = await feed_orient_anchor.load()
    if not anchor:
        snapshot = {"drift": 0.0, "no_anchor": True}
    else:
        try:
            c = await delta_client.centroid(tags_include=["feed-engagement"])
            vec = c.get("centroid")
            if not vec:
                snapshot = {"drift": 0.0, "no_engagement": True}
            else:
                d = crystal_anchor.cosine_distance(anchor["centroid"], vec)
                snapshot = {"drift": round(d, 4)}
        except Exception:
            snapshot = {"drift": 0.0, "error": True}

    now = _now()
    entry = {"t": _iso(now), "v": float(snapshot.get("drift", 0.0))}
    async with _lock:
        state = _load_raw()
        history = state.get("history") or []
        history.append(entry)
        if len(history) > HISTORY_LIMIT:
            history = history[-HISTORY_LIMIT:]
        state["history"] = history
        _save_raw(state)

    return {**snapshot, "sampled_at": entry["t"]}


async def history(since_seconds: int | None = None) -> list[dict]:
    """Return engagement-drift history. Optionally filter to last N seconds."""
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
