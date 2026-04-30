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


def test_html_strip_applies_universally_regardless_of_source() -> None:
    """Any source emitting HTML-laden content gets cleaned up — RSS
    feeds, scraped pages, agent output that wraps in tags, etc."""
    sources_with_html = [
        "rss",
        "rss/self-hosted",
        "fathom-feed",
        "scraper/some-site",
        "claude-code",
        "anything-really",
    ]
    for src in sources_with_html:
        delta = {
            "id": "x",
            "timestamp": "2026-04-29T14:00:00Z",
            "source": src,
            "tags": [],
            "content": (
                '<table><tr><td><a href="https://example.com">'
                "I &amp; Linux: tips &nbsp; tricks</a></td></tr></table>"
            ),
        }
        out = timeline_renderers.render_delta(delta)
        assert "<" not in out, f"HTML leaked for source={src}"
        assert "&amp;" not in out, f"entity leaked for source={src}"
        assert "&nbsp;" not in out, f"entity leaked for source={src}"
        assert "Linux" in out, f"text dropped for source={src}"


def test_html_strip_does_not_eat_plain_comparisons() -> None:
    """Plain text using ``<`` for comparison ("if x < 3") is NOT
    treated as HTML — the heuristic requires a letter or ``/`` after
    the ``<``."""
    delta = {
        "id": "x",
        "timestamp": "2026-04-29T14:00:00Z",
        "source": "claude-code",
        "tags": [],
        "content": "the loop fires when count < 3 and pressure > threshold",
    }
    out = timeline_renderers.render_delta(delta)
    assert "< 3" in out
    assert "> threshold" in out


def test_render_budget_truncates_trailing_strips() -> None:
    """When the cumulative render would exceed the budget, trailing
    strips drop out and a "… N more strips not shown" tail appears."""
    from api.search import _TIMELINE_RENDER_BUDGET_CHARS, _render_timelines

    # Build enough strips that even after per-line truncation, the
    # cumulative render exceeds the budget. The renderer caps each
    # delta line at 130 chars; with header (~50) + anchor line (~150)
    # per strip ≈ ~200 chars/strip. ~50 strips clears 6K easily.
    long_content = "A" * 500
    timelines = []
    for i in range(50):
        timelines.append(
            {
                "id": f"tl_{i}",
                "t_start": f"2026-04-29T14:{i % 60:02d}:00Z",
                "t_end": f"2026-04-29T14:{i % 60:02d}:30Z",
                "anchor_ids": [f"a{i}"],
                "deltas": [
                    {
                        "id": f"a{i}",
                        "timestamp": f"2026-04-29T14:{i % 60:02d}:00Z",
                        "source": "claude-code",
                        "content": long_content,
                        "tags": ["user"],
                        "is_anchor": True,
                    }
                ],
            }
        )
    out = _render_timelines(timelines, query="x")
    assert len(out) <= _TIMELINE_RENDER_BUDGET_CHARS + 200  # tolerate the tail line
    assert "more strip" in out and "not shown" in out


def test_render_budget_does_not_truncate_when_small() -> None:
    """A short render fits within budget and emits no tail."""
    from api.search import _render_timelines

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
                        "content": "short",
                        "tags": ["user"],
                        "is_anchor": True,
                    }
                ],
            }
        ],
        query="x",
    )
    assert "not shown" not in out


def test_render_budget_drops_ambient_before_anchors() -> None:
    """Budget pressure drops ambient-block content first; anchor lines
    in already-emitted strips always emit fully."""
    from api.search import _render_timelines

    long_content = "Z" * 800
    deltas = [
        {
            "id": "anchor",
            "timestamp": "2026-04-29T14:00:00Z",
            "source": "claude-code",
            "content": "the anchor said something specific",
            "tags": ["user"],
            "is_anchor": True,
        }
    ]
    # Pile on ambient that should get clipped.
    for i in range(30):
        deltas.append(
            {
                "id": f"amb{i}",
                "timestamp": f"2026-04-29T14:00:{i:02d}Z",
                "source": "rss/big",
                "content": long_content,
                "tags": [],
                "is_anchor": False,
            }
        )
    out = _render_timelines(
        [
            {
                "id": "tl_0",
                "t_start": "2026-04-29T14:00:00Z",
                "t_end": "2026-04-29T14:00:30Z",
                "anchor_ids": ["anchor"],
                "deltas": deltas,
            }
        ],
        query="x",
    )
    # Anchor always emits, even with hostile ambient.
    assert "the anchor said something specific" in out


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
