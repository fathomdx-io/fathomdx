"""Unit tests for timeline rendering — renderer registry dispatch and the
top-level ``_render_timelines`` markdown shape.

The registry is global; tests touch it sparingly and only via additions
that won't disturb other test modules' state.
"""

from __future__ import annotations

from api import timeline_renderers
from api.search import _format_strip_header, _render_timelines


# ── Renderer dispatch ───────────────────────────────────────────────────


def test_dispatch_kind_field_matches_collapsed_renderer() -> None:
    """Collapsed virtual rows carry ``kind="collapsed"`` as a top-level
    field (not a tag); the dispatch must still find the collapsed
    renderer by treating ``kind:`` keys against that field."""
    delta = {
        "id": "_collapsed_0",
        "kind": "collapsed",
        "source": "agent-heartbeat",
        "count": 12,
        "t_start": "2026-04-29T14:00:00Z",
        "t_end": "2026-04-29T14:05:00Z",
        "tags": [],
        "content": "[heartbeats]",
    }
    out = timeline_renderers.render_delta(delta)
    assert "× 12" in out
    assert "agent-heartbeat" in out


def test_dispatch_source_key_matches_source_field() -> None:
    delta = {
        "id": "x",
        "timestamp": "2026-04-29T14:00:00Z",
        "source": "claude-code",
        "content": "hi",
        "tags": [],
        "is_anchor": False,
    }
    out = timeline_renderers.render_delta(delta)
    # claude-code → dialog renderer; output contains the timestamp and
    # the source label, no role since neither user nor assistant tag.
    assert "claude-code" in out
    assert "14:00:00" in out


def test_dispatch_tag_key_matches_tag_prefix() -> None:
    delta = {
        "id": "s1",
        "timestamp": "2026-04-29T14:00:00Z",
        "source": "fathom-sediment",
        "content": "the take is X. follow-up detail.",
        "tags": ["kind:sediment", "from:abc", "from:def"],
        "is_anchor": False,
    }
    out = timeline_renderers.render_delta(delta)
    assert "from 2 sources" in out
    assert "the take is X" in out


def test_dispatch_falls_through_to_default() -> None:
    delta = {
        "id": "u",
        "timestamp": "2026-04-29T14:00:00Z",
        "source": "rss-hn",
        "content": "Some headline",
        "tags": [],
        "is_anchor": False,
    }
    out = timeline_renderers.render_delta(delta)
    assert "Some headline" in out
    assert out.lstrip().startswith("14:00:00") or "14:00:00" in out


def test_anchor_marker_present() -> None:
    delta = {
        "id": "a",
        "timestamp": "2026-04-29T14:00:00Z",
        "source": "rss-hn",
        "content": "x",
        "tags": [],
        "is_anchor": True,
    }
    out = timeline_renderers.render_delta(delta)
    assert "▸" in out


def test_first_match_wins_ordering() -> None:
    """The registry is iterated in order; an earlier specific match
    short-circuits a later more-general one. Smoke-check by injecting
    a high-priority key, exercising it, then removing it again."""
    sentinel_key = "tag:test-sentinel"
    sentinel_called: list[bool] = []

    def _renderer(_d):
        sentinel_called.append(True)
        return "SENTINEL"

    timeline_renderers._REGISTRY.insert(0, (sentinel_key, _renderer))
    try:
        out = timeline_renderers.render_delta(
            {
                "id": "x",
                "timestamp": "2026-04-29T14:00:00Z",
                "source": "claude-code",
                "tags": ["tag:test-sentinel"],
                "content": "hi",
            }
        )
        assert out == "SENTINEL"
        assert sentinel_called == [True]
    finally:
        timeline_renderers._REGISTRY.pop(0)


# ── _format_strip_header ────────────────────────────────────────────────


def test_strip_header_distinct_times() -> None:
    h = _format_strip_header("2026-04-29T14:24:00Z", "2026-04-29T14:27:30Z")
    assert h == "2026-04-29 · 14:24–14:27"


def test_strip_header_same_minute() -> None:
    h = _format_strip_header("2026-04-29T14:24:08Z", "2026-04-29T14:24:55Z")
    assert h == "2026-04-29 · 14:24"


def test_strip_header_falls_back_for_non_iso() -> None:
    h = _format_strip_header("yesterday", "today")
    assert "yesterday" in h and "today" in h


# ── _render_timelines ───────────────────────────────────────────────────


def test_render_emits_query_header_and_rules() -> None:
    out = _render_timelines(
        [
            {
                "id": "tl_0",
                "t_start": "2026-04-29T14:00:00Z",
                "t_end": "2026-04-29T14:01:00Z",
                "anchor_ids": ["a"],
                "deltas": [
                    {
                        "id": "a",
                        "timestamp": "2026-04-29T14:00:00Z",
                        "source": "claude-code",
                        "content": "hello world",
                        "tags": ["user"],
                        "is_anchor": True,
                    }
                ],
            }
        ],
        query="what happened",
    )
    assert 'your query "what happened"' in out
    assert "═" in out
    assert "▸" in out  # anchor marked
    assert "hello world" in out


def test_render_inserts_link_between_strips() -> None:
    out = _render_timelines(
        [
            {
                "id": "tl_0",
                "t_start": "2026-04-29T14:00:00Z",
                "t_end": "2026-04-29T14:01:00Z",
                "anchor_ids": ["a"],
                "deltas": [
                    {
                        "id": "a",
                        "timestamp": "2026-04-29T14:00:00Z",
                        "source": "claude-code",
                        "content": "first",
                        "tags": [],
                        "is_anchor": True,
                    }
                ],
            },
            {
                "id": "tl_1",
                "t_start": "2026-04-26T11:00:00Z",
                "t_end": "2026-04-26T11:00:30Z",
                "anchor_ids": ["b"],
                "deltas": [
                    {
                        "id": "b",
                        "timestamp": "2026-04-26T11:00:00Z",
                        "source": "claude-code",
                        "content": "second",
                        "tags": [],
                        "is_anchor": True,
                    }
                ],
            },
        ],
        query="x",
    )
    assert "which led to" in out


def test_render_empty_returns_empty_string() -> None:
    assert _render_timelines([], query="anything") == ""


def test_render_includes_collapsed_rows() -> None:
    out = _render_timelines(
        [
            {
                "id": "tl_0",
                "t_start": "2026-04-29T14:00:00Z",
                "t_end": "2026-04-29T14:05:00Z",
                "anchor_ids": ["x"],
                "deltas": [
                    {
                        "id": "_collapsed_0",
                        "kind": "collapsed",
                        "source": "agent-heartbeat",
                        "count": 14,
                        "t_start": "2026-04-29T14:00:00Z",
                        "t_end": "2026-04-29T14:04:30Z",
                        "tags": [],
                        "content": "[hb]",
                    },
                    {
                        "id": "x",
                        "timestamp": "2026-04-29T14:05:00Z",
                        "source": "claude-code",
                        "content": "anchor delta",
                        "tags": [],
                        "is_anchor": True,
                    },
                ],
            }
        ],
        query="x",
    )
    assert "× 14" in out
    assert "anchor delta" in out
