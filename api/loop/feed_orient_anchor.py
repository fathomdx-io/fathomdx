"""Engagement-centroid anchor — a snapshot of the feed-engagement
centroid at the moment a crystal:feed-orient was accepted.

Mirrors api/crystal_anchor.py for the identity crystal, but anchors
on `feed-engagement` deltas instead of all-lake. Engagement-drift is
cosine distance between this anchor and the current engagement
centroid; the anchor refreshes on every successful feed-orient regen,
so drift reads ~0 immediately after a fresh crystal and grows as the
user's engagement diverges from what the crystal predicted.

Storage: single JSON sidecar next to crystal-anchor.json. One anchor
at a time — overwritten on each accepted feed-orient regen.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path

from .._time import now_iso as _now_iso
from ..settings import settings

_lock = asyncio.Lock()


def _path() -> Path:
    base = Path(settings.mood_state_path).parent
    return base / "feed-orient-anchor.json"


def _atomic_write(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".feed-anchor-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


async def save(centroid: list[float], crystal_id: str | None) -> dict:
    """Persist the anchor. Returns the written record."""
    record = {
        "crystal_id": crystal_id,
        "centroid": list(centroid),
        "dim": len(centroid),
        "saved_at": _now_iso(),
    }
    async with _lock:
        _atomic_write(record)
    return record


async def load() -> dict | None:
    async with _lock:
        p = _path()
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except Exception:
            return None
    if not data.get("centroid"):
        return None
    return data
