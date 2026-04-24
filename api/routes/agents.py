"""Agent presence and release-info endpoints.

Agent presence is surfaced via `agent-heartbeat` deltas. An agent
(fathom-agent + its plugins) writes one every ~60s with a 24h
expires_at. The long TTL keeps the heartbeat visible after an agent
goes quiet so the dashboard can render a "disconnected" card; whether
the agent is currently connected is computed from the heartbeat's
timestamp (see HEARTBEAT_STALE_SECONDS in tools.py).
"""
from __future__ import annotations

import json
import time as _time
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter

from .. import delta_client
from .._tags import tag_suffix
from ..tools import heartbeat_age_seconds, heartbeat_is_fresh

router = APIRouter()

# Cache the npm registry's "latest" tag for fathom-agent in process memory.
# Every browser refresh would otherwise hit npm, leaking this install's IP
# and wasting their bandwidth. One hour is plenty — agent releases are slow.
_LATEST_AGENT_CACHE: dict = {"version": None, "checked_at": None, "error": None}
_LATEST_AGENT_TTL_SECONDS = 3600


@router.get("/v1/agents/latest-version")
async def agents_latest_version():
    """Return the newest published fathom-agent version from the npm registry."""
    now = _time.time()
    checked = _LATEST_AGENT_CACHE.get("checked_at")
    if checked and (now - checked) < _LATEST_AGENT_TTL_SECONDS and _LATEST_AGENT_CACHE.get("version"):
        return {
            "latest": _LATEST_AGENT_CACHE["version"],
            "checked_at": datetime.fromtimestamp(checked, UTC).isoformat(),
            "cached": True,
        }
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get("https://registry.npmjs.org/fathom-agent/latest")
            r.raise_for_status()
            data = r.json()
        version = data.get("version")
        _LATEST_AGENT_CACHE.update({"version": version, "checked_at": now, "error": None})
        return {
            "latest": version,
            "checked_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "cached": False,
        }
    except Exception as e:
        _LATEST_AGENT_CACHE.update({"checked_at": now, "error": str(e)})
        return {
            "latest": _LATEST_AGENT_CACHE.get("version"),  # last-known may still be useful
            "checked_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "error": "registry_unreachable",
        }


@router.get("/v1/agents/status")
async def agents_status():
    """Return the most recent heartbeat per host with a connected/disconnected classification.

    Dashboard shows a card for every known host. Fresh heartbeats render as
    connected (glowing robot); stale ones render as disconnected (faded).
    Plugin state travels with the heartbeat so the UI can show badges like
    "routine's permission_mode is not allowed here" without a round trip.
    """
    # Heartbeat deltas linger for 24h. Pull enough to cover realistic fleet
    # sizes — limit=100 easily handles dozens of hosts even with bursty
    # timing where several hosts emit within the same second.
    try:
        deltas = await delta_client.query(limit=100, tags_include=["agent-heartbeat"])
    except Exception as e:
        return {"agents": [], "error": str(e)}

    by_host: dict[str, dict] = {}
    for d in deltas:
        tags = d.get("tags") or []
        host = tag_suffix(tags, "host:") or "unknown"
        ts = d.get("timestamp", "")
        prev = by_host.get(host)
        if prev is None or ts > prev.get("timestamp", ""):
            try:
                payload = json.loads(d.get("content", "{}"))
            except Exception:
                payload = {}
            # Prefer `agent_version` (new, explicit). Fall back to `version`
            # for older agents that only sent the combined field — old agents
            # pinned `version` to the heartbeat schema version (0.10.0), which
            # is what caused the dashboard to show a spurious update chip.
            agent_version = payload.get("agent_version") or payload.get("version")
            age = heartbeat_age_seconds(d)
            by_host[host] = {
                "host": host,
                "timestamp": ts,
                "delta_id": d.get("id"),
                "expires_at": d.get("expires_at"),
                "version": agent_version,
                "schema_version": payload.get("schema_version"),
                "plugins": payload.get("plugins") or {},
                "uptime_s": payload.get("uptime_s"),
                # Local management URL advertised by the agent's local-ui
                # plugin. Only resolvable from the machine itself; dashboard
                # uses it to deep-link the "configure ↗" chip per agent block.
                "agent_url": payload.get("agent_url"),
                # Rotates per agent-process-start. The dashboard's probe
                # compares this to what /api/identity returns, so two
                # agents advertising the same agent_url can still be told
                # apart — the one whose nonce matches is the real target.
                "identity_nonce": payload.get("identity_nonce"),
                "status": "connected" if heartbeat_is_fresh(d) else "disconnected",
                "heartbeat_age_seconds": int(age) if age is not None else None,
            }

    agents = list(by_host.values())
    return {
        "agents": agents,
        # `alive` stays "any known host" so the dashboard shows cards (even
        # disconnected ones) instead of the install view once a host has
        # been seen. Use `connected_count` for "how many are actually up".
        "alive": len(agents) > 0,
        "connected_count": sum(1 for a in agents if a["status"] == "connected"),
    }
