"""Unit tests for PlanExecutor's timeline primitive.

The static helpers (`_gap_trim`, `_collapse_runs`, `_nearest_index`)
don't touch the pool, so they're tested directly. End-to-end execution
goes through `_exec_timeline` which DOES hit asyncpg — those cases use
a tiny stub pool that replays canned rows for the LATERAL query.

Pinned contracts:
  * gap-trim stops at silences > gap_seconds, ignoring max_per_side
  * max_per_side caps independently, gap stays in force after cap
  * collapse folds runs of ≥2 same-source deltas in collapse_sources
  * collapse leaves singletons in collapse_sources untouched
  * windows whose ranges are within merge_gap_seconds merge on output
  * collapsed virtual rows carry kind="collapsed" and count
  * is_anchor is set true on rows whose id is in anchor_ids
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from deltas.models import PlanStep
from deltas.plan import PlanExecutor


# ── _gap_trim ──────────────────────────────────────────────────────────


def _ts(off_seconds: int) -> str:
    base = datetime(2026, 4, 29, 14, 0, 0, tzinfo=UTC)
    return (base + timedelta(seconds=off_seconds)).isoformat()


def test_gap_trim_no_gaps_returns_full_range() -> None:
    rows = [
        {"id": "a", "timestamp": _ts(0)},
        {"id": "b", "timestamp": _ts(10)},
        {"id": "c", "timestamp": _ts(20)},
        {"id": "d", "timestamp": _ts(30)},
    ]
    trimmed = PlanExecutor._gap_trim(
        rows, anchor_idx=2, gap_seconds=60, max_per_side=10
    )
    assert [d["id"] for d in trimmed] == ["a", "b", "c", "d"]


def test_gap_trim_stops_at_left_silence() -> None:
    rows = [
        {"id": "a", "timestamp": _ts(0)},
        {"id": "b", "timestamp": _ts(120)},  # 120s gap from a
        {"id": "c", "timestamp": _ts(130)},  # anchor
        {"id": "d", "timestamp": _ts(140)},
    ]
    trimmed = PlanExecutor._gap_trim(
        rows, anchor_idx=2, gap_seconds=60, max_per_side=10
    )
    # 'a' is past a 120s silence; should be excluded.
    assert [d["id"] for d in trimmed] == ["b", "c", "d"]


def test_gap_trim_stops_at_right_silence() -> None:
    rows = [
        {"id": "a", "timestamp": _ts(0)},  # anchor
        {"id": "b", "timestamp": _ts(10)},
        {"id": "c", "timestamp": _ts(20)},
        {"id": "d", "timestamp": _ts(200)},  # 180s gap
    ]
    trimmed = PlanExecutor._gap_trim(
        rows, anchor_idx=0, gap_seconds=60, max_per_side=10
    )
    assert [d["id"] for d in trimmed] == ["a", "b", "c"]


def test_gap_trim_max_per_side_independent_of_gap() -> None:
    """Even with no gaps, max_per_side caps how many sit either side."""
    rows = [{"id": str(i), "timestamp": _ts(i * 5)} for i in range(11)]
    trimmed = PlanExecutor._gap_trim(
        rows, anchor_idx=5, gap_seconds=120, max_per_side=2
    )
    assert [d["id"] for d in trimmed] == ["3", "4", "5", "6", "7"]


def test_gap_trim_anchor_at_edge() -> None:
    rows = [
        {"id": "a", "timestamp": _ts(0)},  # anchor at start
        {"id": "b", "timestamp": _ts(5)},
        {"id": "c", "timestamp": _ts(10)},
    ]
    trimmed = PlanExecutor._gap_trim(
        rows, anchor_idx=0, gap_seconds=60, max_per_side=10
    )
    assert [d["id"] for d in trimmed] == ["a", "b", "c"]


# ── _collapse_runs ─────────────────────────────────────────────────────


def test_collapse_folds_run_of_two() -> None:
    rows = [
        {"id": "a", "source": "claude-code", "timestamp": _ts(0)},
        {"id": "b", "source": "agent-heartbeat", "timestamp": _ts(2)},
        {"id": "c", "source": "agent-heartbeat", "timestamp": _ts(4)},
        {"id": "d", "source": "claude-code", "timestamp": _ts(6)},
    ]
    out = PlanExecutor._collapse_runs(rows, {"agent-heartbeat"})
    assert [d.get("id") for d in out] == ["a", "_collapsed_0", "d"]
    collapsed = out[1]
    assert collapsed.get("kind") == "collapsed"
    assert collapsed.get("count") == 2
    assert collapsed.get("source") == "agent-heartbeat"
    assert collapsed.get("t_start") == _ts(2)
    assert collapsed.get("t_end") == _ts(4)


def test_collapse_leaves_singletons_alone() -> None:
    """A single same-source delta surrounded by different sources stays
    as a real row — collapse needs at least 2 to fold."""
    rows = [
        {"id": "a", "source": "claude-code", "timestamp": _ts(0)},
        {"id": "b", "source": "agent-heartbeat", "timestamp": _ts(2)},
        {"id": "c", "source": "claude-code", "timestamp": _ts(4)},
    ]
    out = PlanExecutor._collapse_runs(rows, {"agent-heartbeat"})
    assert [d.get("id") for d in out] == ["a", "b", "c"]


def test_collapse_passes_through_non_listed_sources() -> None:
    """Sources not in collapse_sources never fold even in long runs."""
    rows = [
        {"id": "a", "source": "claude-code", "timestamp": _ts(0)},
        {"id": "b", "source": "claude-code", "timestamp": _ts(2)},
        {"id": "c", "source": "claude-code", "timestamp": _ts(4)},
    ]
    out = PlanExecutor._collapse_runs(rows, {"agent-heartbeat"})
    assert [d.get("id") for d in out] == ["a", "b", "c"]


def test_collapse_handles_multiple_runs() -> None:
    rows = [
        {"id": "h1", "source": "agent-heartbeat", "timestamp": _ts(0)},
        {"id": "h2", "source": "agent-heartbeat", "timestamp": _ts(2)},
        {"id": "u1", "source": "claude-code", "timestamp": _ts(4)},
        {"id": "h3", "source": "agent-heartbeat", "timestamp": _ts(6)},
        {"id": "h4", "source": "agent-heartbeat", "timestamp": _ts(8)},
        {"id": "h5", "source": "agent-heartbeat", "timestamp": _ts(10)},
    ]
    out = PlanExecutor._collapse_runs(rows, {"agent-heartbeat"})
    assert [d.get("id") for d in out] == ["_collapsed_0", "u1", "_collapsed_1"]
    assert out[0].get("count") == 2
    assert out[2].get("count") == 3


def test_collapse_empty_input() -> None:
    assert PlanExecutor._collapse_runs([], {"x"}) == []


def test_collapse_no_sources_listed_is_passthrough() -> None:
    rows = [{"id": "a", "source": "x", "timestamp": _ts(0)}]
    assert PlanExecutor._collapse_runs(rows, set()) == rows


# ── _nearest_index ─────────────────────────────────────────────────────


def test_nearest_index_picks_closest() -> None:
    rows = [
        {"id": "a", "timestamp": _ts(0)},
        {"id": "b", "timestamp": _ts(50)},
        {"id": "c", "timestamp": _ts(100)},
    ]
    target = datetime(2026, 4, 29, 14, 0, 0, tzinfo=UTC) + timedelta(seconds=60)
    assert PlanExecutor._nearest_index(rows, target) == 1  # 50 closer than 100


def test_nearest_index_empty_returns_none() -> None:
    assert PlanExecutor._nearest_index([], datetime.now(UTC)) is None


# ── End-to-end _exec_timeline (mocked pool) ────────────────────────────


class _FakeRecord(dict):
    """asyncpg.Record-like — supports both attribute and item access in
    the small ways `_row_to_dict` and the seed-grouping loop use."""


class _FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.last_args: tuple = ()

    async def fetch(self, sql: str, *args):
        self.last_args = args
        return [_FakeRecord(r) for r in self._rows]


def _exec(pool: _FakePool) -> PlanExecutor:
    return PlanExecutor(pool=pool, embed_fn=lambda _t: [0.0])


def _row(seed_id: str, did: str, off_s: int, source: str = "claude-code") -> dict:
    """Synthetic LATERAL row including the seed_id pass-through."""
    return {
        "seed_id": seed_id,
        "id": did,
        "timestamp": datetime(2026, 4, 29, 14, 0, 0, tzinfo=UTC)
        + timedelta(seconds=off_s),
        "modality": "text",
        "content": f"line {did}",
        "source": source,
        "tags": [],
        "media_hash": None,
        "expires_at": None,
        "gap_seconds": float(off_s),
    }


async def _run_timeline(rows: list[dict], seeds: list[dict], **step_kwargs):
    pool = _FakePool(rows)
    step = PlanStep(id="tl", timeline="parent", **step_kwargs)
    return await _exec(pool)._exec_timeline(step, seeds)


def test_timeline_builds_window_around_anchor() -> None:
    import asyncio

    seed_ts = "2026-04-29T14:00:30+00:00"
    seeds = [{"id": "anchor", "timestamp": seed_ts, "source": "claude-code"}]
    rows = [
        _row("anchor", "before", 20),
        _row("anchor", "anchor", 30),
        _row("anchor", "after", 40),
    ]
    out = asyncio.run(
        _run_timeline(
            rows,
            seeds,
            radius_minutes=20,
            max_per_side=10,
            gap_minutes=30,
            merge_gap_seconds=300,
            collapse_sources=[],
        )
    )
    assert len(out) == 1
    tl = out[0]
    ids = [d.id for d in tl["deltas"]]
    assert ids == ["before", "anchor", "after"]
    assert tl["anchor_ids"] == ["anchor"]
    by_id = {d.id: d for d in tl["deltas"]}
    assert by_id["anchor"].is_anchor is True
    assert by_id["before"].is_anchor is False


def test_timeline_collapses_high_freq_sources() -> None:
    import asyncio

    seeds = [
        {"id": "anchor", "timestamp": "2026-04-29T14:00:30+00:00", "source": "claude-code"}
    ]
    rows = [
        _row("anchor", "h1", 10, source="agent-heartbeat"),
        _row("anchor", "h2", 12, source="agent-heartbeat"),
        _row("anchor", "h3", 14, source="agent-heartbeat"),
        _row("anchor", "anchor", 30),
        _row("anchor", "after", 40),
    ]
    out = asyncio.run(
        _run_timeline(
            rows,
            seeds,
            radius_minutes=20,
            max_per_side=10,
            gap_minutes=30,
            collapse_sources=["agent-heartbeat"],
        )
    )
    assert len(out) == 1
    deltas = out[0]["deltas"]
    kinds = [d.kind for d in deltas]
    # First entry is the collapsed run, then anchor, then after.
    assert kinds == ["collapsed", None, None]
    assert deltas[0].count == 3


def test_timeline_merges_close_windows() -> None:
    import asyncio

    seeds = [
        {"id": "a1", "timestamp": "2026-04-29T14:00:30+00:00", "source": "claude-code"},
        {"id": "a2", "timestamp": "2026-04-29T14:01:30+00:00", "source": "claude-code"},
    ]
    rows = [
        _row("a1", "a1", 30),
        _row("a1", "x", 40),
        _row("a2", "y", 80),
        _row("a2", "a2", 90),
    ]
    out = asyncio.run(
        _run_timeline(
            rows,
            seeds,
            radius_minutes=5,
            max_per_side=10,
            gap_minutes=30,
            merge_gap_seconds=120,
            collapse_sources=[],
        )
    )
    assert len(out) == 1
    tl = out[0]
    assert sorted(tl["anchor_ids"]) == ["a1", "a2"]
    ids = [d.id for d in tl["deltas"]]
    assert ids == ["a1", "x", "y", "a2"]


def test_timeline_keeps_far_windows_separate() -> None:
    import asyncio

    seeds = [
        {"id": "a1", "timestamp": "2026-04-29T14:00:30+00:00", "source": "claude-code"},
        {"id": "a2", "timestamp": "2026-04-29T14:30:00+00:00", "source": "claude-code"},
    ]
    rows = [
        _row("a1", "a1", 30),
        _row("a2", "a2", 1800),
    ]
    out = asyncio.run(
        _run_timeline(
            rows,
            seeds,
            radius_minutes=5,
            max_per_side=10,
            gap_minutes=30,
            merge_gap_seconds=120,
            collapse_sources=[],
        )
    )
    assert len(out) == 2
    assert out[0]["anchor_ids"] == ["a1"]
    assert out[1]["anchor_ids"] == ["a2"]
