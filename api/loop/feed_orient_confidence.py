"""Feed-orient confidence — predicted-vs-actual for engagement signal.

After each crystal:feed-orient regen, the engagement centroid is
snapshotted as the anchor (see feed_orient_anchor). Confidence asks:
"how well does this anchor predict the user's reactions?"

For each feed-engagement delta written after the latest regen:
  • Compute similarity(engagement_embedding, anchor_centroid).
    High similarity = the crystal's distribution predicted this kind
    of card was in-bounds; low similarity = the crystal didn't.
  • Combine with the engagement's kind:
      + or chat → score = similarity         (hit when sim is high)
      −         → score = 1 − similarity     (hit when sim is low —
                                              user correctly rejected
                                              an out-of-distribution
                                              card)
  • Mean across all post-regen engagements = current confidence.

Confidence climbs toward 1 when the crystal is matching the user's
reactions; falls toward 0 when prediction and reality diverge —
which is the trigger the regen-predicate watches for.
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
ENGAGEMENT_FETCH_LIMIT: int = 200

_lock = asyncio.Lock()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _path() -> Path:
    base = Path(settings.mood_state_path).parent
    return base / "feed-confidence-history.json"


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
    fd, tmp = tempfile.mkstemp(prefix=".feed-conf-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


async def _latest_regen_ts() -> str | None:
    """Timestamp of the most recent crystal:feed-orient. Confidence is
    measured *over engagements since this regen*, so the score resets
    its sample window each time the crystal refreshes."""
    try:
        items = await delta_client.query(
            tags_include=["crystal:feed-orient"],
            limit=1,
        )
    except Exception:
        return None
    return items[0].get("timestamp") if items else None


def _engagement_kind(d: dict) -> str:
    for t in d.get("tags") or []:
        if isinstance(t, str) and t.startswith("engagement:"):
            return t.split(":", 1)[1]
    return ""


def _score(kind: str, similarity: float) -> float | None:
    """Map (kind, similarity) → 0..1 score. None if kind has no signal."""
    sim = max(0.0, min(1.0, similarity))
    if kind in ("more", "chat"):
        return sim
    if kind == "less":
        return 1.0 - sim
    return None


async def sample() -> dict:
    """Compute current confidence and append to history."""
    anchor = await feed_orient_anchor.load()
    regen_ts = await _latest_regen_ts()

    if not anchor:
        snapshot = {"confidence": None, "n": 0, "no_anchor": True}
    elif not regen_ts:
        snapshot = {"confidence": None, "n": 0, "no_crystal": True}
    else:
        try:
            engagements = await delta_client.query(
                tags_include=["feed-engagement"],
                time_start=regen_ts,
                limit=ENGAGEMENT_FETCH_LIMIT,
            )
        except Exception:
            engagements = []
        anchor_vec = anchor.get("centroid") or []
        scores: list[float] = []
        for e in engagements:
            emb = e.get("embedding") or []
            if not emb or not anchor_vec or len(emb) != len(anchor_vec):
                continue
            kind = _engagement_kind(e)
            sim = 1.0 - crystal_anchor.cosine_distance(anchor_vec, emb)
            s = _score(kind, sim)
            if s is None:
                continue
            scores.append(s)
        if scores:
            snapshot = {
                "confidence": round(sum(scores) / len(scores), 4),
                "n": len(scores),
            }
        else:
            snapshot = {"confidence": None, "n": 0, "no_engagements": True}

    now = _now()
    entry = {"t": _iso(now), "v": snapshot.get("confidence")}
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
    """Return confidence history. Optionally filter to last N seconds."""
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
