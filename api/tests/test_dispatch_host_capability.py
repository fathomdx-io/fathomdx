"""Helpers surfaced to the witness for `dispatch_helper` are derived
from the `helper-role:<role>` and `helper-desc:<role>:<text>` tags on
recent heartbeats. Each plugin self-registers its role(s); the witness
filter just reports what the heartbeat tags say.

Latest-heartbeat-per-host is authoritative: a host that just disabled
its only helper-role plugin drops immediately, even if older beats in
the 5-minute window still carried the role.
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


def _beat(
    host: str,
    *,
    plugins: list[str] = (),
    helper_roles: list[tuple[str, str]] = (),
    ts: str = "2026-04-30T17:00:00Z",
) -> dict:
    """Synthetic heartbeat — host + plugin + helper-role tags.

    helper_roles is a list of (role, description) tuples. Plugins are
    advertised with `plugin:<name>` tags but the new dispatch filter
    keys off `helper-role:<role>` / `helper-desc:<role>:<text>` instead.

    delta_client returns newest-first; tests order the list to mimic
    that.
    """
    tags = [
        "agent-heartbeat",
        "fathom-agent",
        f"host:{host}",
        *[f"plugin:{p}" for p in plugins],
    ]
    for role, desc in helper_roles:
        tags.append(f"helper-role:{role}")
        if desc:
            tags.append(f"helper-desc:{role}:{desc}")
    return {
        "id": f"beat-{host}-{ts}",
        "timestamp": ts,
        "tags": tags,
    }


async def test_helper_role_host_is_listed(_patched_heartbeats):
    from api.loop.witness import _available_helpers

    _patched_heartbeats["beats"] = [
        _beat(
            "myras-fedora-laptop",
            plugins=["heartbeat", "kitty", "vault"],
            helper_roles=[("claude-code", "shell, file edits, git")],
        ),
    ]

    helpers = await _available_helpers()
    assert helpers == [
        {
            "host": "myras-fedora-laptop",
            "role": "claude-code",
            "description": "shell, file edits, git",
        }
    ]


async def test_host_without_any_role_is_excluded(_patched_heartbeats):
    """A heartbeating host that advertises no helper-role tag has no
    dispatch-capable plugin enabled and shouldn't be listed."""
    from api.loop.witness import _available_helpers

    _patched_heartbeats["beats"] = [
        _beat(
            "myras-fedora-laptop",
            plugins=["heartbeat", "kitty"],
            helper_roles=[("claude-code", "")],
        ),
        _beat("nixos-server", plugins=["heartbeat", "localui"]),  # no helper roles
    ]

    helpers = await _available_helpers()
    hosts = {h["host"] for h in helpers}
    assert hosts == {"myras-fedora-laptop"}
    assert "nixos-server" not in hosts


async def test_empty_when_nothing_advertises_a_role(_patched_heartbeats):
    from api.loop.witness import _available_helpers

    _patched_heartbeats["beats"] = [
        _beat("nixos-server", plugins=["heartbeat", "localui"]),
    ]
    helpers = await _available_helpers()
    assert helpers == []


async def test_latest_heartbeat_wins_when_role_drops(_patched_heartbeats):
    """A host that JUST disabled its claude-code role drops out
    immediately on its next beat. Older beats in the 5-min window
    mustn't keep it alive — that would create a window where the
    witness happily dispatches to a host that can no longer accept
    the task.

    `delta_client.query` returns newest-first, so the first beat
    seen per host wins.
    """
    from api.loop.witness import _available_helpers

    _patched_heartbeats["beats"] = [
        # Newest beat: claude-code role gone.
        _beat("flaky-host", plugins=["heartbeat"], ts="2026-04-30T17:04:00Z"),
        # Older beat in the same window: claude-code was advertised.
        _beat(
            "flaky-host",
            plugins=["heartbeat", "kitty"],
            helper_roles=[("claude-code", "")],
            ts="2026-04-30T17:00:00Z",
        ),
    ]

    helpers = await _available_helpers()
    assert helpers == []


async def test_latest_heartbeat_wins_when_role_added(_patched_heartbeats):
    """Mirror case: a host that JUST enabled claude-code is available
    immediately, even though older beats in the window lacked it."""
    from api.loop.witness import _available_helpers

    _patched_heartbeats["beats"] = [
        # Newest: claude-code just enabled.
        _beat(
            "warming-up",
            plugins=["heartbeat", "kitty"],
            helper_roles=[("claude-code", "shell")],
            ts="2026-04-30T17:04:00Z",
        ),
        # Older: no roles advertised.
        _beat("warming-up", plugins=["heartbeat"], ts="2026-04-30T17:00:00Z"),
    ]

    helpers = await _available_helpers()
    assert [h["host"] for h in helpers] == ["warming-up"]
    assert helpers[0]["role"] == "claude-code"


async def test_multiple_roles_on_same_host_are_listed_separately(_patched_heartbeats):
    """A host advertising both claude-code and openclaw appears twice
    in the helpers list — once per (host, role) pair. Sorted
    alphabetically by (role, host) so claude-code lands first."""
    from api.loop.witness import _available_helpers

    _patched_heartbeats["beats"] = [
        _beat(
            "myras-fedora-laptop",
            plugins=["heartbeat", "kitty", "openclaw"],
            helper_roles=[
                ("claude-code", "shell, files"),
                ("openclaw", "chat-synthesis"),
            ],
        ),
    ]
    helpers = await _available_helpers()
    assert [(h["role"], h["host"]) for h in helpers] == [
        ("claude-code", "myras-fedora-laptop"),
        ("openclaw", "myras-fedora-laptop"),
    ]
    descs = {h["role"]: h["description"] for h in helpers}
    assert descs["claude-code"] == "shell, files"
    assert descs["openclaw"] == "chat-synthesis"


async def test_alphabetical_sort_puts_claude_code_above_openclaw(_patched_heartbeats):
    """Ordering matters — claude-code should naturally sort above
    openclaw so the model's left-to-right reading prefers it when
    capability overlaps."""
    from api.loop.witness import _available_helpers

    _patched_heartbeats["beats"] = [
        _beat(
            "host-a",
            plugins=["heartbeat", "openclaw"],
            helper_roles=[("openclaw", "")],
        ),
        _beat(
            "host-b",
            plugins=["heartbeat", "kitty"],
            helper_roles=[("claude-code", "")],
        ),
    ]
    helpers = await _available_helpers()
    # Sorted by (role, host): claude-code @ host-b first, then openclaw @ host-a.
    assert [(h["role"], h["host"]) for h in helpers] == [
        ("claude-code", "host-b"),
        ("openclaw", "host-a"),
    ]
