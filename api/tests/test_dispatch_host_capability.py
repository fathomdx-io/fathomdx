"""Hosts surfaced to the witness for `claude-code:<host>` dispatch
must self-report kitty as a plugin. A host that's heartbeating but
has no kitty plugin can't actually spawn a claude-code task — it
shouldn't appear in the witness's MACHINES list.

Latest-heartbeat-per-host is authoritative: a host that just
disabled kitty is excluded immediately, even if older beats in the
5-minute window still showed the plugin enabled.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def _patched_heartbeats(monkeypatch):
    """Stub `delta_client.query` so we can stage heartbeat shapes."""
    from api import delta_client

    state: dict[str, list[dict]] = {"beats": []}

    async def _query(tags_include=None, time_start=None, limit=100, **_kw):
        if tags_include and "agent-heartbeat" in tags_include:
            return list(state["beats"])
        return []

    monkeypatch.setattr(delta_client, "query", _query)
    return state


def _beat(host: str, plugins: list[str], ts: str = "2026-04-30T17:00:00Z") -> dict:
    """Synthetic heartbeat — only the bits the host filter reads.

    delta_client returns newest-first; tests order the list to mimic
    that.
    """
    return {
        "id": f"beat-{host}-{ts}",
        "timestamp": ts,
        "tags": [
            "agent-heartbeat",
            "fathom-agent",
            f"host:{host}",
            *[f"plugin:{p}" for p in plugins],
        ],
    }


async def test_kitty_capable_host_is_listed(_patched_heartbeats):
    from api.loop.witness import _available_claude_code_hosts

    _patched_heartbeats["beats"] = [
        _beat("myras-fedora-laptop", ["heartbeat", "kitty", "vault"]),
    ]

    hosts = await _available_claude_code_hosts()
    assert hosts == ["myras-fedora-laptop"]


async def test_non_kitty_host_is_excluded(_patched_heartbeats):
    """A host that's heartbeating but lacks kitty can't actually spawn
    a task. Don't list it as a dispatch target — the witness was
    routing to it and the dispatch silently no-op'd."""
    from api.loop.witness import _available_claude_code_hosts

    _patched_heartbeats["beats"] = [
        _beat("myras-fedora-laptop", ["heartbeat", "kitty"]),
        _beat("nixos-server",        ["heartbeat", "localui"]),  # no kitty
    ]

    hosts = await _available_claude_code_hosts()
    assert hosts == ["myras-fedora-laptop"]
    assert "nixos-server" not in hosts


async def test_empty_when_nothing_kitty_capable(_patched_heartbeats):
    from api.loop.witness import _available_claude_code_hosts

    _patched_heartbeats["beats"] = [
        _beat("nixos-server", ["heartbeat", "localui"]),
    ]
    hosts = await _available_claude_code_hosts()
    assert hosts == []


async def test_latest_heartbeat_wins_when_capability_drops(_patched_heartbeats):
    """A host that JUST disabled kitty drops out immediately on its
    next beat. Older beats in the 5-min window mustn't keep it alive —
    that would create a window where the witness happily dispatches to
    a host that can no longer accept the task.

    `delta_client.query` returns newest-first, so the first beat seen
    per host wins.
    """
    from api.loop.witness import _available_claude_code_hosts

    _patched_heartbeats["beats"] = [
        # Newest beat: kitty disabled.
        _beat("flaky-host", ["heartbeat"], ts="2026-04-30T17:04:00Z"),
        # Older beat in the same window: kitty was on.
        _beat("flaky-host", ["heartbeat", "kitty"], ts="2026-04-30T17:00:00Z"),
    ]

    hosts = await _available_claude_code_hosts()
    assert hosts == []


async def test_latest_heartbeat_wins_when_capability_added(_patched_heartbeats):
    """Mirror case: a host that JUST enabled kitty is available
    immediately, even though older beats in the window lacked it."""
    from api.loop.witness import _available_claude_code_hosts

    _patched_heartbeats["beats"] = [
        # Newest: kitty just enabled.
        _beat("warming-up", ["heartbeat", "kitty"], ts="2026-04-30T17:04:00Z"),
        # Older: kitty was off.
        _beat("warming-up", ["heartbeat"], ts="2026-04-30T17:00:00Z"),
    ]

    hosts = await _available_claude_code_hosts()
    assert hosts == ["warming-up"]
