"""LLM-down gate — per-tier "is this currently working?" answer.

Reads two signals from the lake:

  * Newest `system-error system-error-tier:<tier>` delta — most recent
    failure attributable to that tier. Written by the threaded harness
    on rate-limit / unreachable / etc., and by mood synthesis on its
    own LLM-error path.

  * Newest `kind:llm-heartbeat llm-tier:<tier>` delta — written by
    `api/loop/llm.py` after every successful `loop_generate*` return,
    debounced to 30s. Carries a 1h TTL so it ages out naturally during
    real outages.

A tier is "down" when its newest error is more recent than its newest
heartbeat (or when there's an error and no heartbeat at all). Used by:

  * Background producers (mood, feed-orient, witness, sediment) — skip
    silently when the gate says down rather than burn another call.
  * The threaded supervisor — when hard is down, probes at most once
    per `LLM_PROBE_INTERVAL_S` instead of one fire per user message.
  * `/v1/llm/status` — reuses `tier_status()` so the dashboard banner
    and the gate cannot drift apart.

Results are cached briefly per tier; recovery latency is bounded by
`_CACHE_TTL_S`. Cache invalidation after a fire is the caller's
responsibility — see `invalidate_cache()`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

from .. import delta_client

log = logging.getLogger(__name__)

VALID_TIERS: tuple[str, ...] = ("hard", "medium")

# Cache TTL for the lake reads. Short enough that recovery is detected
# quickly; long enough that a busy tick (multiple producers all checking
# in the same second) doesn't re-query for each one.
_CACHE_TTL_S = 5.0

# Probe budget — how often a probe-gated caller (e.g., the threaded
# supervisor) may attempt one fire while a tier is down. The fire is the
# probe; success writes a heartbeat that flips the gate, failure writes
# a fresh error that keeps it down.
LLM_PROBE_INTERVAL_S = float(os.getenv("LLM_PROBE_INTERVAL_S", "60"))


# tier -> (cached_at_monotonic, err_delta, heartbeat_delta)
_state_cache: dict[str, tuple[float, dict | None, dict | None]] = {}
_state_lock = asyncio.Lock()

# Probe budget — stateful, in-process. Keyed by caller-chosen string so
# multiple probe-gated paths can coexist without stomping each other.
_last_probe_at: dict[str, float] = {}


async def _newest_with_tags(tags: list[str]) -> dict | None:
    """Return the newest delta carrying ALL `tags`, or None on empty/error."""
    try:
        rows = await delta_client.query(tags_include=tags, limit=1)
    except Exception:
        log.debug("llm-gate: query failed for %s", tags, exc_info=True)
        return None
    return rows[0] if rows else None


async def _read_state(tier: str) -> tuple[dict | None, dict | None]:
    """Cached read of (newest_error, newest_heartbeat) for `tier`."""
    now = time.monotonic()
    cached = _state_cache.get(tier)
    if cached and (now - cached[0]) < _CACHE_TTL_S:
        return cached[1], cached[2]
    async with _state_lock:
        cached = _state_cache.get(tier)
        if cached and (now - cached[0]) < _CACHE_TTL_S:
            return cached[1], cached[2]
        err = await _newest_with_tags(["system-error", f"system-error-tier:{tier}"])
        ok = await _newest_with_tags(["kind:llm-heartbeat", f"llm-tier:{tier}"])
        _state_cache[tier] = (now, err, ok)
        return err, ok


def _ts(d: dict | None) -> str:
    return (d or {}).get("timestamp") or ""


async def is_down(tier: str) -> bool:
    """Fast yes/no — is this tier currently in an error state?"""
    if tier not in VALID_TIERS:
        return False
    err, ok = await _read_state(tier)
    if not err:
        return False
    return not ok or _ts(err) > _ts(ok)


async def tier_status(tier: str) -> dict:
    """Full snapshot for `/v1/llm/status` and similar surfaces.

    Returns `{ok, error, last_heartbeat_ts, last_error_ts}` where
    `error` is the parsed payload of the newest error delta when down,
    else None.
    """
    if tier not in VALID_TIERS:
        return {"ok": True, "error": None, "last_heartbeat_ts": None, "last_error_ts": None}
    err, ok = await _read_state(tier)
    err_ts, ok_ts = _ts(err), _ts(ok)
    down = bool(err_ts) and (not ok_ts or err_ts > ok_ts)
    payload: dict | None = None
    if down and err is not None:
        try:
            payload = json.loads(err.get("content") or "{}")
        except Exception:
            payload = {"message": err.get("content") or ""}
    return {
        "ok": not down,
        "error": payload,
        "last_heartbeat_ts": ok_ts or None,
        "last_error_ts": err_ts or None,
    }


def claim_probe(key: str, interval_s: float | None = None) -> bool:
    """Claim a probe budget slot for `key`.

    Returns True at most once per `interval_s` (default
    `LLM_PROBE_INTERVAL_S`) per key. The caller is responsible for
    actually doing the probe — this only tracks whether the budget
    allows one right now. Distinct keys have independent budgets, so a
    supervisor probe and a manual-trigger probe don't share a slot.
    """
    interval = interval_s if interval_s is not None else LLM_PROBE_INTERVAL_S
    now = time.monotonic()
    last = _last_probe_at.get(key, 0.0)
    if now - last < interval:
        return False
    _last_probe_at[key] = now
    return True


def invalidate_cache(tier: str | None = None) -> None:
    """Drop the cached state for one tier (or all). Call after writing
    a fresh error/heartbeat delta to make the next check see it."""
    if tier is None:
        _state_cache.clear()
    else:
        _state_cache.pop(tier, None)


def reset_probe_budget(key: str | None = None) -> None:
    """Drop probe-budget tracking for one key (or all). Mainly for tests."""
    if key is None:
        _last_probe_at.clear()
    else:
        _last_probe_at.pop(key, None)
