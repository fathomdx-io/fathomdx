"""Retrieval event log — counts deltas flowing OUT of the lake.

Every call to a retrieval endpoint (/search, /search/image, /plan,
/deltas query) records one event: {t: iso, n: count} where n is the
number of deltas returned. On read, events are bucketed and n summed
to produce a "deltas retrieved per bucket" timeline — symmetric with
the write-side usage counter that buckets actual delta writes.

Stored as a rolling JSON event log, capped at EVENT_LIMIT. Mirrors the
pattern used by consumer-fathom/api/recall.py, but lives at the lake
itself so every client (consumer-api, loop-api, CLI, MCP) is counted
uniformly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("delta-store.retrievals")

EVENT_LIMIT: int = 5000

_PATH = Path(os.environ.get("RETRIEVALS_PATH", "/data/retrievals-history.json"))
_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _load_raw() -> dict:
    if not _PATH.exists():
        return {"events": []}
    try:
        return json.loads(_PATH.read_text())
    except Exception:
        return {"events": []}


def _save_raw(state: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".retrievals-", dir=str(_PATH.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, _PATH)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


async def record(n: int) -> None:
    """Record a retrieval event — n deltas returned at this moment."""
    if n <= 0:
        return
    async with _lock:
        state = _load_raw()
        events = state.get("events") or []
        events.append({"t": _iso(_now()), "n": int(n)})
        if len(events) > EVENT_LIMIT:
            events = events[-EVENT_LIMIT:]
        state["events"] = events
        try:
            _save_raw(state)
        except Exception:
            log.warning("Failed to persist retrievals log", exc_info=True)


def fire_and_forget(n: int) -> None:
    """Schedule record(n) from a sync-or-async context. Best-effort, never raises."""
    if n <= 0:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(record(n))


async def history(since_seconds: int, buckets: int = 60) -> list[dict]:
    """Return bucketed delta-retrieval counts across the window.

    Each entry: {t: iso, v: int}. v sums `n` for events falling in that bucket.
    Shape matches usage.history so the UI can plot both series side by side.
    """
    if since_seconds <= 0 or buckets <= 0:
        return []
    async with _lock:
        state = _load_raw()
        events = list(state.get("events") or [])

    now = _now()
    start = now - timedelta(seconds=since_seconds)
    bucket_seconds = since_seconds / buckets
    counts = [0] * buckets
    for e in events:
        ts = _parse(e.get("t"))
        if not ts:
            continue
        offset = (ts - start).total_seconds()
        if offset < 0:
            continue
        idx = int(offset / bucket_seconds)
        if idx >= buckets:
            idx = buckets - 1
        counts[idx] += int(e.get("n", 1))

    out: list[dict] = []
    for i, c in enumerate(counts):
        tick = start + timedelta(seconds=bucket_seconds * (i + 0.5))
        out.append({"t": _iso(tick), "v": c})
    return out
