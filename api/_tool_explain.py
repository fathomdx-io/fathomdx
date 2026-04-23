"""The `explain` tool — static doc strings + live state for dashboard concepts.

Moved out of api/tools.py so the dispatcher file stays under the
size-sanity ceiling. Only the execute_explain entry point is
imported from tools.py; everything else is file-local.
"""

from __future__ import annotations

import json

import httpx

from . import delta_client
from .settings import settings

# ── Explain tool ────────────────────────────────────────────────────────────
#
# Static doc + live state, per dashboard concept. Each topic builder returns
# a dict; the tool serializes it. Live calls stay best-effort — if the
# source-runner is down we still return the static doc so the LLM has
# something useful to relay.


_EXPLAIN_SOURCES = (
    "SOURCES — pollers that write deltas into the lake on a schedule.\n"
    "Each source is a plugin running inside the source-runner container:\n"
    "  rss           — fetch feed items, one delta per entry\n"
    "  mastodon      — a user's timeline, one delta per toot\n"
    "  hacker-news   — front-page stories above a karma threshold\n"
    "  custom        — user-defined, wraps any HTTP endpoint\n\n"
    "Sources show up as chips under the 'Sources' section on the dashboard. "
    "Click + Add source to configure one. Configured sources can be paused, "
    "resumed, or manually polled from their detail view. Deltas a source "
    "writes get tagged with the source type + instance id, so you can filter "
    "for them later (e.g. tags_include=['source:rss', 'rss:hn-front'])."
)


_EXPLAIN_FEED = (
    "FEED ('What I noticed') — the dashboard's top surface, right below the "
    "opener. Each card is a synthesized *story* composed from a cluster of "
    "recent deltas: the feed worker groups related lake activity and writes "
    "a narrative delta with a title, body, and optional images.\n\n"
    "Stories are generated lazily from lake content — on a fresh install "
    "with no deltas there's nothing to synthesize, which is why new users "
    "see an empty-state prompt until their first sources or chats land.\n\n"
    "Tapping a card opens it as a new chat session with the story as the "
    "opening turn, so the user can dig into any thread Fathom noticed."
)


_EXPLAIN_STATS = (
    "STATS — a multi-track time-series of Fathom's internal state, rendered "
    "like an ECG at the bottom of the dashboard. Each track is a different "
    "signal sampled over the last N hours:\n"
    "  deltas       — writes per time bucket (ingest rate)\n"
    "  recall       — how many deltas got pulled back out via search\n"
    "  mood         — carrier-wave pressure (how 'loud' things feel)\n"
    "  drift        — semantic drift between identity crystal and current state\n"
    "  usage        — LLM token spend per bucket\n\n"
    "Stats is the 'am I alive and well?' view — a glance shows whether "
    "sources are flowing, whether Fathom is recalling, whether pressure is "
    "building toward a mood synthesis. The drift track in particular "
    "triggers auto-regeneration of the identity crystal when it crosses "
    "threshold × red_ratio (see settings.crystal_*)."
)


_EXPLAIN_AGENT = (
    "AGENT — the local Node process that runs on each connected machine. "
    "It emits passive observations into the lake on a schedule (sysinfo, "
    "vault watchers, homeassistant feeds) and executes routines when their "
    "cron schedules fire. Routines are the way you reach a machine: a "
    "prompt + cron + workspace, and the local agent spawns a claude-code "
    "subprocess in a kitty window to do the work.\n\n"
    "Without a body connected, you still have your mind (chat, memory, "
    "feed), but routines can't execute — they need a body to run on.\n\n"
    "Install a new body: main dashboard → Agent section → pick Linux / "
    "Mac / Windows → run the one-liner. Each body writes an "
    "agent-heartbeat delta every ~60s tagged host:<hostname>, so you "
    "know which ones are alive."
)


async def _live_sources_summary() -> dict:
    """Best-effort: fetch configured sources from source-runner."""
    try:
        async with httpx.AsyncClient(
            base_url=settings.source_runner_url.rstrip("/"), timeout=5
        ) as c:
            r = await c.get("/api/sources")
            r.raise_for_status()
            data = r.json() or {}
    except Exception:
        return {"configured": None, "note": "source-runner unreachable"}

    items = data.get("sources") or data if isinstance(data, list) else data.get("sources", [])
    if not isinstance(items, list):
        items = []
    configured = [s for s in items if s.get("status") != "available"]
    by_status: dict[str, int] = {}
    for s in configured:
        st = s.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
    return {
        "configured": len(configured),
        "by_status": by_status,
        "types": sorted({s.get("source_type") for s in configured if s.get("source_type")}),
    }


async def _live_feed_summary() -> dict:
    try:
        data = await delta_client.feed_stories(limit=50, offset=0)
    except Exception:
        return {"stories": None, "note": "feed endpoint unreachable"}
    return {"stories": len(data.get("stories") or [])}


async def _live_stats_summary() -> dict:
    try:
        s = await delta_client.stats()
    except Exception:
        return {"note": "stats endpoint unreachable"}
    return {
        "total_deltas": s.get("total_deltas") or s.get("count"),
        "embedded": s.get("embedded") or s.get("embedded_count"),
        "embedding_coverage": s.get("embedding_coverage"),
    }


async def _live_agent_summary() -> dict:
    # Lazy import to avoid a circular: tools.py imports this module at
    # module load for the _execute_explain entry point, so we can't
    # import _agent_alive at top level without re-entering tools.py.
    from .tools import _agent_alive

    alive, agents = await _agent_alive()
    return {
        "connected": alive,
        "count": len(agents),
        "hosts": [a["host"] for a in agents],
    }


async def _execute_explain(args: dict) -> str:
    topic = (args.get("topic") or "").strip().lower()
    if topic == "sources":
        return json.dumps(
            {
                "topic": topic,
                "doc": _EXPLAIN_SOURCES,
                "live": await _live_sources_summary(),
            }
        )
    if topic == "feed":
        return json.dumps(
            {
                "topic": topic,
                "doc": _EXPLAIN_FEED,
                "live": await _live_feed_summary(),
            }
        )
    if topic == "stats":
        return json.dumps(
            {
                "topic": topic,
                "doc": _EXPLAIN_STATS,
                "live": await _live_stats_summary(),
            }
        )
    if topic == "agent":
        return json.dumps(
            {
                "topic": topic,
                "doc": _EXPLAIN_AGENT,
                "live": await _live_agent_summary(),
            }
        )
    return json.dumps(
        {
            "topic": topic,
            "error": "unknown_topic",
            "known": ["sources", "feed", "stats", "agent"],
        }
    )
