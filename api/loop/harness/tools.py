"""Harness tools — what Fathom can call from inside the agentic loop.

Nine peer tools, each addressing a different way of seeing or shaping
the lake:

  semantic       — LLM-composed multi-step search plan over embeddings.
                   The heavy hitter; expensive and powerful. Use when
                   the question has a CONTENT anchor that can be named.
  expand         — graph traversal: fetch children of a provenance delta.
  ascend         — graph traversal: find provenance that contains a delta.
  deliberate     — synthesis: parliament voices on a question. Expensive,
                   for genuine antagonism only.
  state          — current attention: intents, proposals, mood, crystal,
                   recent activity. The puddle's home turf. Call
                   state(action='help') to discover sub-actions.
  pattern        — aggregations + structural queries: tag filters, counts,
                   salience rankings, dormant signals. Call
                   pattern(action='help').
  time           — temporal-window queries: between dates, group-by-day.
                   Call time(action='help').
  relate         — engagement + relational: who, what's been affirmed,
                   what's been dropped. Call relate(action='help').
  propose_provenance — write a provenance PROPOSAL into the dashboard
                   feed for human review. Use when the working set in
                   this fire reveals a coherent stretch worth naming
                   (an episode, a topic, an era). Drafts; doesn't write
                   real provenance — the operator approves.

Most tools surface delta ids — feed them into expand/ascend/
semantic to navigate further. Lenses surface; graph tools traverse;
propose_provenance shapes the lake's structure.

`deliberate` wraps the existing convener + parliament one-round path; it
does NOT reimplement deliberation. The harness inverts the relationship —
deliberation is now elective, called from within the harness when the
question calls for antagonism, instead of being a mandatory pre-step.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from ... import delta_client
from ... import search as search_mod
from ..intents import CONVO_TAG, intent_kind, pending_intents
from ..puddle import puddle

# Hard caps on per-call result counts — these limit how many items a
# tool returns, not how much rendered text it produces. The harness
# test page wants to see full results; the prompt-budget truncation
# happens later in render_tool_history when the result is folded back
# into the next turn's prompt.
_EXPAND_LIMIT = 12
_ASCEND_LIMIT = 6


# ─── search ────────────────────────────────────────────────────────────


async def tool_search(*, query: str, depth: str = "deep") -> str:
    """Run the canonical NL search and return its `as_prompt` rendering.

    Same `api/search.py:search()` the loop's intent-searcher and the
    chat surface use. Timeline view by default — strips of moments
    around each hit, not orphan deltas.
    """
    if not query or not query.strip():
        return "ERROR: empty query"
    if depth not in ("shallow", "deep"):
        depth = "deep"
    try:
        result = await search_mod.search(text=query.strip(), depth=depth, view="timeline")
    except Exception as e:
        return f"ERROR: search failed — {type(e).__name__}: {e}"
    rendered = (result.get("as_prompt") or "").strip()
    if not rendered:
        return "(no results — query did not surface anything from the lake)"
    return rendered


# ─── expand ────────────────────────────────────────────────────────────


async def tool_expand(*, delta_id: str) -> str:
    """Fetch the moments a sediment/provenance summarizes.

    Reads the sediment's `from:<id>` tags and pulls those source deltas.
    Works on any delta with `from:` pointers, but is most useful on
    `kind:sediment` (compositional-recall summaries).
    """
    delta_id = (delta_id or "").strip()
    if not delta_id:
        return "ERROR: delta_id is empty"
    try:
        delta = await delta_client.get_delta(delta_id)
    except Exception as e:
        return f"ERROR: could not fetch delta {delta_id[:12]} — {type(e).__name__}: {e}"
    if not isinstance(delta, dict) or not delta.get("id"):
        return f"ERROR: delta {delta_id[:12]} not found"

    from_ids: list[str] = []
    for tag in delta.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("from:"):
            tid = tag.split(":", 1)[1].strip()
            if tid and tid not in from_ids:
                from_ids.append(tid)
    if not from_ids:
        return (
            f"(delta {delta_id[:12]} has no `from:` pointers — "
            f"not a sediment, or sediment with no recorded sources)"
        )

    try:
        sources = await delta_client.batch_get(from_ids[:_EXPAND_LIMIT])
    except Exception as e:
        return f"ERROR: batch_get failed — {type(e).__name__}: {e}"

    if not sources:
        return f"(no sources resolvable for {delta_id[:12]} — they may have been reaped)"

    blocks: list[str] = [f"Sources of {delta_id[:12]} ({len(sources)} of {len(from_ids)}):", ""]
    for s in sources:
        blocks.append(_render_delta_brief(s))
    if len(from_ids) > _EXPAND_LIMIT:
        blocks.append(f"  …({len(from_ids) - _EXPAND_LIMIT} more sources not shown)")
    return "\n".join(blocks)


# ─── ascend ────────────────────────────────────────────────────────────


async def tool_ascend(*, delta_id: str) -> str:
    """Find sediment/provenance that contains this delta.

    Queries the lake for both `kind:sediment` (reactive recall-driven)
    AND `kind:provenance` (intentional hierarchical) deltas carrying
    `from:<delta_id>`. Returns the parent — none, one, or several (a
    moment can belong to multiple parents; many-to-many is the design).

    The provenance kind carries a `provenance-level:N` tag — level:1
    is an episode, level:2 is a topic spanning episodes, level:3 is
    an era spanning topics. Ascending repeatedly walks UP the hierarchy.
    """
    delta_id = (delta_id or "").strip()
    if not delta_id:
        return "ERROR: delta_id is empty"
    parents: list[dict] = []
    seen: set[str] = set()
    for kind in ("kind:provenance", "kind:sediment"):
        try:
            hits = await delta_client.query(
                tags_include=[kind, f"from:{delta_id}"],
                limit=_ASCEND_LIMIT,
            )
        except Exception as e:
            return f"ERROR: ascend query failed — {type(e).__name__}: {e}"
        for h in hits or []:
            hid = h.get("id") or ""
            if hid and hid not in seen:
                seen.add(hid)
                parents.append(h)
    if not parents:
        return (
            f"(no provenance contains {delta_id[:12]} — either it's standalone "
            f"or no sediment/provenance has been written that cites it yet)"
        )
    blocks: list[str] = [f"Containing provenance for {delta_id[:12]} ({len(parents)}):", ""]
    for p in parents:
        blocks.append(_render_delta_brief(p))
    return "\n".join(blocks)


# ─── deliberate ────────────────────────────────────────────────────────


async def tool_deliberate(
    *,
    question: str,
    session_tag: str = "",
    pending: list[dict] | None = None,
    standpoint: Any = None,
) -> str:
    """Run a parliament round on a question — return voices' takes.

    Self-contained via `harness/deliberate.py`: hardcoded trimurti
    (creator / preserver / destroyer), N parallel LLM calls, no
    convener pre-pass and no substrate gathering — the harness already
    has substrate via its other tools.

    `session_tag`, `pending`, and `standpoint` are accepted for
    signature compatibility with the harness dispatcher (which injects
    them automatically) but are unused in this implementation. The
    deliberate machinery operates only on the question.
    """
    from .deliberate import deliberate as _deliberate

    return await _deliberate(question=question)


# ─── shared helpers ────────────────────────────────────────────────────


def _render_delta_brief(d: dict) -> str:
    """One-block rendering of a delta — id prefix, source, ts, full content.

    Used by `expand` and `ascend` so their results read consistently.
    No truncation — the harness shows everything that's there.
    """
    did = (d.get("id") or "")[:12] or "?"
    src = d.get("source") or "lake"
    ts = (d.get("timestamp") or d.get("created_at") or "")[:19]
    content = (d.get("content") or "").strip()
    tag_summary = ""
    tags = d.get("tags") or []
    kind_tags = [t for t in tags if isinstance(t, str) and t.startswith("kind:")]
    if kind_tags:
        tag_summary = f" [{', '.join(kind_tags)}]"
    media = (d.get("media_hash") or "").strip()
    media_line = f"\n      [Image attached: media_hash={media}]" if media else ""
    return f"  · {did} {src} {ts}{tag_summary}\n      {content}{media_line}"


# ─── lens tools: state / pattern / time / relate ──────────────────────


_LENS_RESULT_LIMIT = 30  # cap items returned per lens call


def _short(d: dict, *, max_chars: int | None = None) -> str:
    """One-block render of a delta — what every lens uses to show its
    results. Untruncated by default; pass max_chars only when a caller
    explicitly wants a snippet (rare)."""
    did = (d.get("id") or "")[:12] or "?"
    src = d.get("source") or "lake"
    ts = (d.get("timestamp") or d.get("created_at") or "")[:19]
    content = (d.get("content") or "").strip()
    if max_chars is not None and len(content) > max_chars:
        content = content[:max_chars] + "…"
    tags = d.get("tags") or []
    salient = [
        t
        for t in tags
        if isinstance(t, str) and (t.startswith("kind:") or t.startswith("provenance-level:"))
    ]
    tag_part = f" [{', '.join(salient)}]" if salient else ""
    media = (d.get("media_hash") or "").strip()
    media_line = f"\n      [Image attached: media_hash={media}]" if media else ""
    return f"  · {did} {src} {ts}{tag_part}\n      {content}{media_line}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso_or_none(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _hours_ago_iso(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


# ─── state lens ────────────────────────────────────────────────────────


_STATE_HELP = """state — current-attention queries. What's alive right now.

Actions:
  state(action="help")              — this menu
  state(action="pending_intents")   — the loop's pending queue (puddle)
  state(action="proposals")         — kind:proposal pending review (lake)
  state(action="mood")              — current mood deltas from the puddle
  state(action="crystal")           — identity facets (most recent)
  state(action="recent",
        hours=<N>, group_by="source") — what's written across sources lately

Returned items always include delta ids — feed them into expand/ascend
or search to navigate further."""


async def tool_state(*, action: str = "help", **kwargs) -> str:
    action = (action or "").strip().lower()
    if action in ("", "help"):
        return _STATE_HELP

    if action == "pending_intents":
        try:
            items = pending_intents()
        except Exception as e:
            return f"ERROR: pending_intents failed — {type(e).__name__}: {e}"
        if not items:
            return "(queue empty — no pending intents)"
        items = items[:_LENS_RESULT_LIMIT]
        blocks = [f"Pending intents ({len(items)}):", ""]
        for it in items:
            kind = intent_kind(it)
            content = (it.get("content") or "").strip().split("\n\n[intent-payload]", 1)[0]
            iid = (it.get("id") or "")[:12]
            ts = (it.get("timestamp") or "")[:19]
            blocks.append(f"  · {iid} kind={kind} {ts}\n      {content}")
        return "\n".join(blocks)

    if action == "proposals":
        try:
            items = await delta_client.query(
                tags_include=["kind:proposal", "proposal-status:pending"],
                limit=_LENS_RESULT_LIMIT,
            )
        except Exception as e:
            return f"ERROR: proposal query failed — {type(e).__name__}: {e}"
        if not items:
            return "(no pending proposals)"
        blocks = [f"Pending proposals ({len(items)}):", ""]
        for d in items:
            tool = ""
            for t in d.get("tags") or []:
                if t.startswith("tool:"):
                    tool = t.split(":", 1)[1]
                    break
            try:
                payload = json.loads(d.get("content") or "{}")
            except Exception:
                payload = {}
            title = payload.get("title") or (d.get("content") or "")
            did = (d.get("id") or "")[:12]
            ts = (d.get("timestamp") or "")[:19]
            blocks.append(f"  · {did} tool={tool} {ts}\n      {title}")
        return "\n".join(blocks)

    if action == "mood":
        try:
            items = puddle.query(tags_include=[CONVO_TAG, "mood"], limit=20)
        except Exception as e:
            return f"ERROR: mood query failed — {type(e).__name__}: {e}"
        if not items:
            # Fallback to lake — telepathy may not have mirrored mood
            # if it's offline (e.g. FATHOM_QUIET_MODE).
            try:
                items = await delta_client.query(
                    tags_include=["kind:mood"],
                    limit=20,
                )
            except Exception:
                items = []
        if not items:
            return "(no mood deltas surfaced — substrate may be empty)"
        blocks = [f"Mood ({len(items)}):", ""]
        for d in items:
            blocks.append(_short(d))
        return "\n".join(blocks)

    if action == "crystal":
        try:
            items = await delta_client.query(
                tags_include=["identity-crystal"],
                limit=20,
            )
        except Exception as e:
            return f"ERROR: crystal query failed — {type(e).__name__}: {e}"
        if not items:
            return "(no identity-crystal deltas in the lake)"
        blocks = [f"Identity crystal facets ({len(items)}):", ""]
        for d in items:
            blocks.append(_short(d))
        return "\n".join(blocks)

    if action == "recent":
        try:
            hours = float(kwargs.get("hours") or 12)
        except (TypeError, ValueError):
            hours = 12
        group_by = (kwargs.get("group_by") or "source").strip().lower()
        try:
            items = await delta_client.query(
                tags_include=[],
                time_start=_hours_ago_iso(hours),
                limit=300,
            )
        except Exception as e:
            return f"ERROR: recent query failed — {type(e).__name__}: {e}"
        if not items:
            return f"(no activity in the last {hours}h)"

        if group_by == "source":
            counts = Counter((d.get("source") or "?") for d in items)
            blocks = [
                f"Activity in last {hours}h ({len(items)} deltas, {len(counts)} sources):",
                "",
            ]
            for src, n in counts.most_common(20):
                samples = [d for d in items if d.get("source") == src][:5]
                blocks.append(f"  ── {src} · {n} deltas ──")
                for d in samples:
                    content = (d.get("content") or "").strip()
                    blocks.append(f"      {(d.get('id') or '')[:12]} {content}")
            return "\n".join(blocks)

        if group_by == "kind":
            counts: Counter = Counter()
            for d in items:
                for t in d.get("tags") or []:
                    if isinstance(t, str) and t.startswith("kind:"):
                        counts[t] += 1
                        break
            blocks = [f"Activity in last {hours}h grouped by kind:", ""]
            for k, n in counts.most_common(20):
                blocks.append(f"  · {k}: {n}")
            return "\n".join(blocks)

        return f"ERROR: unknown group_by={group_by!r} (try 'source' or 'kind')"

    return f"ERROR: unknown state action {action!r} — try state(action='help')"


# ─── pattern lens ──────────────────────────────────────────────────────


_PATTERN_HELP = """pattern — aggregations and lake-wide structural queries.

Actions:
  pattern(action="help")
  pattern(action="tagged", tag="<tag>", since="<iso>", limit=<N>)
       — direct tag filter. e.g. tag="kind:todo".
  pattern(action="count_by", group_by="source"|"kind", since="<iso>")
       — counts across the lake.
  pattern(action="salient_recent", hours=<N>)
       — recent witness cards ranked by judge axes (salience+resonance+
         confidence). What you've been deeply engaged with lately.
  pattern(action="dormant", silent_for_days=<N>, min_chars=<N>)
       — old, content-rich deltas that haven't been retrieved lately.
         Useful for "what have I forgotten."

Returned items include delta ids — feed them into expand/ascend/search."""


async def tool_pattern(*, action: str = "help", **kwargs) -> str:
    action = (action or "").strip().lower()
    if action in ("", "help"):
        return _PATTERN_HELP

    if action == "tagged":
        tag = (kwargs.get("tag") or "").strip()
        if not tag:
            return "ERROR: pattern.tagged requires `tag`"
        since = (kwargs.get("since") or "").strip() or None
        try:
            limit = int(kwargs.get("limit") or _LENS_RESULT_LIMIT)
        except (TypeError, ValueError):
            limit = _LENS_RESULT_LIMIT
        limit = max(1, min(100, limit))
        q: dict = {"tags_include": [tag], "limit": limit}
        if since:
            q["time_start"] = since
        try:
            items = await delta_client.query(**q)
        except Exception as e:
            return f"ERROR: tag query failed — {type(e).__name__}: {e}"
        if not items:
            return f"(nothing tagged {tag!r} in the queried window)"
        blocks = [f"Tagged {tag!r} ({len(items)}):", ""]
        for d in items[:limit]:
            blocks.append(_short(d))
        return "\n".join(blocks)

    if action == "count_by":
        group_by = (kwargs.get("group_by") or "source").strip().lower()
        since = (kwargs.get("since") or "").strip() or _hours_ago_iso(24 * 7)
        try:
            items = await delta_client.query(
                tags_include=[],
                time_start=since,
                limit=2000,
            )
        except Exception as e:
            return f"ERROR: count query failed — {type(e).__name__}: {e}"
        if not items:
            return f"(no deltas since {since})"
        if group_by == "source":
            counts = Counter((d.get("source") or "?") for d in items)
        elif group_by == "kind":
            counts = Counter()
            for d in items:
                for t in d.get("tags") or []:
                    if isinstance(t, str) and t.startswith("kind:"):
                        counts[t] += 1
                        break
        elif group_by == "level":
            counts = Counter()
            for d in items:
                lvl = "?"
                for t in d.get("tags") or []:
                    if isinstance(t, str) and t.startswith("provenance-level:"):
                        lvl = t.split(":", 1)[1]
                        break
                counts[f"level:{lvl}"] += 1
        else:
            return f"ERROR: unknown group_by={group_by!r} (try source/kind/level)"
        total = sum(counts.values())
        blocks = [
            f"Counts since {since[:19]} ({total} deltas, group_by={group_by}):",
            "",
        ]
        for k, n in counts.most_common(30):
            pct = (n / total) * 100.0 if total else 0.0
            blocks.append(f"  · {k}: {n}  ({pct:.1f}%)")
        return "\n".join(blocks)

    if action == "salient_recent":
        try:
            hours = float(kwargs.get("hours") or 24)
        except (TypeError, ValueError):
            hours = 24
        try:
            cards = await delta_client.query(
                tags_include=["feed-card"],
                time_start=_hours_ago_iso(hours),
                limit=200,
            )
        except Exception as e:
            return f"ERROR: salient query failed — {type(e).__name__}: {e}"
        if not cards:
            return f"(no feed-cards in last {hours}h)"

        # Pull judge axes deltas in the same window so we can score.
        try:
            axes_deltas = await delta_client.query(
                tags_include=["kind:judge-axes"],
                time_start=_hours_ago_iso(hours + 1),
                limit=400,
            )
        except Exception:
            axes_deltas = []
        axes_by_card: dict[str, dict] = {}
        for ad in axes_deltas:
            for t in ad.get("tags") or []:
                if isinstance(t, str) and t.startswith("for-card:"):
                    cid = t.split(":", 1)[1]
                    with contextlib.suppress(Exception):
                        axes_by_card[cid] = json.loads(ad.get("content") or "{}")
                    break

        scored: list[tuple[float, dict, dict]] = []
        for c in cards:
            cid = c.get("id") or ""
            ax = axes_by_card.get(cid) or {}
            sal = float(ax.get("salience") or 0.0)
            res = float(ax.get("resonance") or 0.0)
            conf = float(ax.get("confidence") or 0.0)
            score = (sal + res + conf) / 3.0
            scored.append((score, c, ax))
        scored.sort(key=lambda t: t[0], reverse=True)
        blocks = [f"Salient cards in last {hours}h:", ""]
        for score, c, ax in scored[:_LENS_RESULT_LIMIT]:
            try:
                payload = json.loads(c.get("content") or "{}")
            except Exception:
                payload = {}
            title = payload.get("title") or payload.get("body") or ""
            did = (c.get("id") or "")[:12]
            blocks.append(
                f"  · {did} score={score:.2f} "
                f"(s={ax.get('salience', 0):.2f} r={ax.get('resonance', 0):.2f} "
                f"c={ax.get('confidence', 0):.2f})\n      {title}"
            )
        return "\n".join(blocks)

    if action == "dormant":
        try:
            silent_days = float(kwargs.get("silent_for_days") or 60)
        except (TypeError, ValueError):
            silent_days = 60
        try:
            min_chars = int(kwargs.get("min_chars") or 200)
        except (TypeError, ValueError):
            min_chars = 200
        # Cheap heuristic: pick deltas older than `silent_days`, with
        # content_len >= min_chars. Lacking retrieval-count metadata
        # client-side, we approximate "dormant" as "old + substantive."
        # When delta-store grows a retrieval-count surface this can
        # tighten to "old + substantive + low-touched."
        cutoff = (datetime.now(UTC) - timedelta(days=silent_days)).isoformat()
        try:
            items = await delta_client.query(
                tags_include=[],
                limit=300,
            )
        except Exception as e:
            return f"ERROR: dormant query failed — {type(e).__name__}: {e}"
        old = [
            d
            for d in items
            if (d.get("timestamp") or "") < cutoff and len(d.get("content") or "") >= min_chars
        ]
        if not old:
            return (
                f"(nothing dormant — no substantive deltas older than "
                f"{silent_days} days surfaced in the recent slice. The lake "
                f"reaper may have culled them, or this query needs a wider net.)"
            )
        old.sort(key=lambda d: d.get("timestamp") or "")
        blocks = [
            f"Dormant deltas (older than {silent_days}d, >={min_chars} chars, {len(old)} found):",
            "",
        ]
        for d in old[:_LENS_RESULT_LIMIT]:
            blocks.append(_short(d))
        return "\n".join(blocks)

    return f"ERROR: unknown pattern action {action!r} — try pattern(action='help')"


# ─── time lens ─────────────────────────────────────────────────────────


_TIME_HELP = """time — temporal-window queries.

Actions:
  time(action="help")
  time(action="between", start="<iso>", end="<iso>",
       source="<src>", tag="<tag>", limit=<N>)
       — pull deltas in a time window. Optional source/tag filters.
  time(action="bucket_by", period="day"|"hour"|"week",
       since="<iso>", group_by="source"|"kind")
       — counts grouped by time bucket (e.g. activity per day).
  time(action="around", delta_id="<id>", gap_minutes=30)
       — given a hit, return the chronologically-surrounding moments
         from the same source, gap-bounded. Use this AFTER any lens
         tool (state / pattern / relate / time.between) returns
         interesting deltas — the lens gives you the matches, this
         gives you the context they sat in. Mirrors the timeline-strip
         shape semantic search returns by default.

Returned items include delta ids — feed them into expand/ascend/semantic."""


async def tool_time(*, action: str = "help", **kwargs) -> str:
    action = (action or "").strip().lower()
    if action in ("", "help"):
        return _TIME_HELP

    if action == "between":
        start = (kwargs.get("start") or "").strip()
        end = (kwargs.get("end") or "").strip()
        if not start:
            return "ERROR: time.between requires `start` (ISO timestamp)"
        if not end:
            end = _now_iso()
        source = (kwargs.get("source") or "").strip() or None
        tag = (kwargs.get("tag") or "").strip() or None
        try:
            limit = int(kwargs.get("limit") or _LENS_RESULT_LIMIT)
        except (TypeError, ValueError):
            limit = _LENS_RESULT_LIMIT
        limit = max(1, min(200, limit))
        q: dict = {
            "tags_include": [tag] if tag else [],
            "time_start": start,
            "limit": limit,
        }
        if source:
            q["source"] = source
        try:
            items = await delta_client.query(**q)
        except Exception as e:
            return f"ERROR: between query failed — {type(e).__name__}: {e}"
        # Client-side filter on end (delta_client.query only takes time_start).
        items = [d for d in (items or []) if (d.get("timestamp") or "") <= end]
        if not items:
            return f"(no deltas in {start}..{end})"
        blocks = [f"Window {start[:19]}..{end[:19]} ({len(items)}):", ""]
        for d in items[:limit]:
            blocks.append(_short(d))
        return "\n".join(blocks)

    if action == "bucket_by":
        period = (kwargs.get("period") or "day").strip().lower()
        since = (kwargs.get("since") or "").strip() or _hours_ago_iso(24 * 14)
        group_by = (kwargs.get("group_by") or "").strip().lower() or None
        if period not in ("hour", "day", "week"):
            return f"ERROR: unknown period={period!r} (try hour/day/week)"
        try:
            items = await delta_client.query(
                tags_include=[],
                time_start=since,
                limit=3000,
            )
        except Exception as e:
            return f"ERROR: bucket query failed — {type(e).__name__}: {e}"
        if not items:
            return f"(no deltas since {since})"

        def _bucket(ts: str) -> str:
            if not ts or len(ts) < 10:
                return "?"
            if period == "hour":
                return ts[:13]  # YYYY-MM-DDTHH
            if period == "week":
                # ISO calendar week
                try:
                    dt = datetime.fromisoformat((ts[:-1] + "+00:00") if ts.endswith("Z") else ts)
                    iso = dt.isocalendar()
                    return f"{iso.year}-W{iso.week:02d}"
                except Exception:
                    return "?"
            return ts[:10]  # day

        if group_by:
            nested: dict[str, Counter] = {}
            for d in items:
                b = _bucket(d.get("timestamp") or "")
                if group_by == "source":
                    sub = d.get("source") or "?"
                elif group_by == "kind":
                    sub = "?"
                    for t in d.get("tags") or []:
                        if isinstance(t, str) and t.startswith("kind:"):
                            sub = t
                            break
                else:
                    return f"ERROR: unknown group_by={group_by!r}"
                nested.setdefault(b, Counter())[sub] += 1
            blocks = [
                f"Bucketed by {period} grouped by {group_by} since {since[:19]}:",
                "",
            ]
            for b in sorted(nested.keys()):
                top = nested[b].most_common(5)
                line = ", ".join(f"{k}={n}" for k, n in top)
                blocks.append(f"  {b}  {line}")
            return "\n".join(blocks)

        counts = Counter(_bucket(d.get("timestamp") or "") for d in items)
        blocks = [f"Counts by {period} since {since[:19]}:", ""]
        for b in sorted(counts.keys()):
            blocks.append(f"  {b}  {counts[b]}")
        return "\n".join(blocks)

    if action == "around":
        anchor_id = (kwargs.get("delta_id") or "").strip()
        if not anchor_id:
            return "ERROR: time.around requires `delta_id`"
        try:
            gap_minutes = float(kwargs.get("gap_minutes") or 30)
        except (TypeError, ValueError):
            gap_minutes = 30
        try:
            anchor = await delta_client.get_delta(anchor_id)
        except Exception as e:
            return f"ERROR: could not fetch anchor — {type(e).__name__}: {e}"
        if not isinstance(anchor, dict) or not anchor.get("id"):
            return f"ERROR: anchor {anchor_id[:12]} not found"
        anchor_ts_str = anchor.get("timestamp") or anchor.get("created_at") or ""
        anchor_dt = _parse_iso_or_none(anchor_ts_str)
        if anchor_dt is None:
            return f"ERROR: anchor {anchor_id[:12]} has no parseable timestamp"
        anchor_source = anchor.get("source") or ""

        # Pull a generous window centered on the anchor in the same
        # source. delta_client.query takes time_start; we filter end
        # client-side.
        window_minutes = max(gap_minutes * 4, 240)  # generous; we'll trim
        win_start = (anchor_dt - timedelta(minutes=window_minutes)).isoformat()
        win_end_dt = anchor_dt + timedelta(minutes=window_minutes)
        try:
            items = await delta_client.query(
                tags_include=[],
                time_start=win_start,
                source=anchor_source if anchor_source else None,
                limit=400,
            )
        except Exception as e:
            return f"ERROR: window query failed — {type(e).__name__}: {e}"
        items = [
            d
            for d in (items or [])
            if (d.get("timestamp") or "") <= win_end_dt.isoformat()
            and (not anchor_source or (d.get("source") or "") == anchor_source)
        ]
        if not items:
            return (
                f"(no surrounding moments for {anchor_id[:12]} in source "
                f"{anchor_source!r} within ±{int(window_minutes)}min)"
            )

        # Sort chronologically, then walk outward from anchor pulling in
        # neighbors until a gap of `gap_minutes` breaks the strip.
        for d in items:
            d["_dt"] = _parse_iso_or_none(d.get("timestamp") or "")
        items = [d for d in items if d.get("_dt") is not None]
        items.sort(key=lambda d: d["_dt"])
        # Find anchor's index (or insert position).
        anchor_idx = None
        for i, d in enumerate(items):
            if d.get("id") == anchor.get("id"):
                anchor_idx = i
                break
        if anchor_idx is None:
            # Anchor not in same source's window — fall back to closest.
            for i, d in enumerate(items):
                if d["_dt"] >= anchor_dt:
                    anchor_idx = i
                    break
            if anchor_idx is None:
                anchor_idx = len(items) - 1

        # Walk back as long as gap < gap_minutes.
        gap = timedelta(minutes=gap_minutes)
        lo = anchor_idx
        while lo > 0:
            prev = items[lo - 1]
            cur = items[lo]
            if (cur["_dt"] - prev["_dt"]) > gap:
                break
            lo -= 1
        # Walk forward.
        hi = anchor_idx
        while hi < len(items) - 1:
            cur = items[hi]
            nxt = items[hi + 1]
            if (nxt["_dt"] - cur["_dt"]) > gap:
                break
            hi += 1
        strip = items[lo : hi + 1]
        # Mark the anchor visually
        anchor_id_full = anchor.get("id") or ""

        blocks: list[str] = [
            f"Surrounding moments for {anchor_id[:12]} in {anchor_source!r} "
            f"(strip: {len(strip)}, gap-bound={gap_minutes}m):",
            "",
        ]
        for d in strip:
            marker = "▸" if d.get("id") == anchor_id_full else " "
            ts = (d.get("timestamp") or "")[:19]
            did = (d.get("id") or "")[:12]
            content = (d.get("content") or "").strip()
            tags = d.get("tags") or []
            kind_tags = [t for t in tags if isinstance(t, str) and t.startswith("kind:")]
            tag_part = f" [{', '.join(kind_tags)}]" if kind_tags else ""
            blocks.append(f"  {marker} {did} {ts}{tag_part}")
            blocks.append(f"      {content}")
        return "\n".join(blocks)

    return f"ERROR: unknown time action {action!r} — try time(action='help')"


# ─── relate lens ───────────────────────────────────────────────────────


_RELATE_HELP = """relate — engagement, contacts, and valence pointers.

Actions:
  relate(action="help")
  relate(action="with_contact", slug="<contact_slug>", limit=<N>)
       — deltas tagged contact:<slug>. Steph, Nova, etc.
  relate(action="engagement", direction="+"|"-", hours=<N>)
       — recent affirmations (+) or refutations (-) you've cast.
  relate(action="dropped_around", delta_id="<id>")
       — anything that refutes:<id> or replied to it negatively.
  relate(action="cited_by", delta_id="<id>")
       — sediment / provenance / engagement-attestation deltas that
         carry from:<id> or affirms:<id>.

Returned items include delta ids — feed them into expand/ascend/search."""


async def tool_relate(*, action: str = "help", **kwargs) -> str:
    action = (action or "").strip().lower()
    if action in ("", "help"):
        return _RELATE_HELP

    if action == "with_contact":
        slug = (kwargs.get("slug") or "").strip()
        if not slug:
            return "ERROR: relate.with_contact requires `slug`"
        try:
            limit = int(kwargs.get("limit") or _LENS_RESULT_LIMIT)
        except (TypeError, ValueError):
            limit = _LENS_RESULT_LIMIT
        limit = max(1, min(100, limit))
        try:
            items = await delta_client.query(
                tags_include=[f"contact:{slug}"],
                limit=limit,
            )
        except Exception as e:
            return f"ERROR: contact query failed — {type(e).__name__}: {e}"
        if not items:
            return f"(no deltas tagged contact:{slug})"
        blocks = [f"Tagged contact:{slug} ({len(items)}):", ""]
        for d in items[:limit]:
            blocks.append(_short(d))
        return "\n".join(blocks)

    if action == "engagement":
        direction = (kwargs.get("direction") or "+").strip()
        if direction not in ("+", "-"):
            return f"ERROR: direction must be '+' or '-' (got {direction!r})"
        try:
            hours = float(kwargs.get("hours") or 24)
        except (TypeError, ValueError):
            hours = 24
        # Engagement-attest deltas carry affirms:<id> or refutes:<id>.
        # Pull recent ones and surface what they pointed at.
        try:
            items = await delta_client.query(
                tags_include=["kind:engagement-attest"],
                time_start=_hours_ago_iso(hours),
                limit=200,
            )
        except Exception as e:
            return f"ERROR: engagement query failed — {type(e).__name__}: {e}"
        prefix = "affirms:" if direction == "+" else "refutes:"
        targets: list[tuple[str, str]] = []
        for d in items:
            for t in d.get("tags") or []:
                if isinstance(t, str) and t.startswith(prefix):
                    targets.append((d.get("id") or "", t.split(":", 1)[1]))
                    break
        if not targets:
            label = "affirmed" if direction == "+" else "refuted"
            return f"(nothing {label} in the last {hours}h)"
        blocks = [
            f"Recent {'+' if direction == '+' else '-'}engagements "
            f"({len(targets)} in last {hours}h):",
            "",
        ]
        for attest_id, target_id in targets[:_LENS_RESULT_LIMIT]:
            blocks.append(f"  · {attest_id[:12]} → {target_id[:12]}")
        return "\n".join(blocks)

    if action == "dropped_around":
        target = (kwargs.get("delta_id") or "").strip()
        if not target:
            return "ERROR: relate.dropped_around requires `delta_id`"
        try:
            items = await delta_client.query(
                tags_include=[f"refutes:{target}"],
                limit=_LENS_RESULT_LIMIT,
            )
        except Exception as e:
            return f"ERROR: refutes query failed — {type(e).__name__}: {e}"
        if not items:
            return f"(nothing refutes {target[:12]})"
        blocks = [f"Refutations of {target[:12]} ({len(items)}):", ""]
        for d in items[:_LENS_RESULT_LIMIT]:
            blocks.append(_short(d))
        return "\n".join(blocks)

    if action == "cited_by":
        target = (kwargs.get("delta_id") or "").strip()
        if not target:
            return "ERROR: relate.cited_by requires `delta_id`"
        seen: set[str] = set()
        items: list[dict] = []
        for tag in (f"from:{target}", f"affirms:{target}"):
            try:
                hits = await delta_client.query(
                    tags_include=[tag],
                    limit=_LENS_RESULT_LIMIT,
                )
            except Exception:
                continue
            for d in hits or []:
                did = d.get("id") or ""
                if did and did not in seen:
                    seen.add(did)
                    items.append(d)
        if not items:
            return f"(nothing cites {target[:12]})"
        blocks = [f"Citations of {target[:12]} ({len(items)}):", ""]
        for d in items[:_LENS_RESULT_LIMIT]:
            blocks.append(_short(d))
        return "\n".join(blocks)

    return f"ERROR: unknown relate action {action!r} — try relate(action='help')"


# ─── propose_provenance ────────────────────────────────────────────────


async def tool_propose_provenance(
    *,
    level: int | None = None,
    title: str = "",
    summary: str = "",
    from_ids: list | None = None,
    rationale: str = "",
    test_questions: list | None = None,
    session_tag: str = "",
) -> str:
    """Draft a provenance proposal into the dashboard feed.

    Use when the working set in this fire reveals a coherent stretch
    that doesn't yet have a name — an episode (level 1), a topic
    spanning episodes (level 2), or an era (level 3). The proposal
    lands in the feed as `kind:proposal tool:provenance`; the operator
    sees Edit/Deny/Approve. On approve, the existing proposals.py
    handler writes the real `kind:provenance` delta with the same args.

    SIZE DISCIPLINE — keep provenance focused, never bulk:
      · L1 episode: 5-30 base moments. Tightly related — a single
        episode that happened. If you're past 30 you're naming a
        topic, not an episode.
      · L2 topic: 3-10 L1 episodes (plus optionally a few base-moment
        stragglers that didn't fit any episode). A recurring concern
        that spans episodes.
      · L3 era: 3-10 L2 topics (plus optionally a few stragglers).
        An arc large enough you'd point to it when telling the story
        of a season.
      · Total fan-out 5-30 per node REGARDLESS of level. Don't make
        giant containers. If you'd be naming a stretch with 50+
        constituents, that's two stretches that need decomposing.

    DOES NOT write provenance directly — only a proposal. This keeps
    the model from polluting the hierarchy with low-confidence
    inferences. If you're confident enough to write directly, you're
    confident enough to wait for the operator to approve.

    APPEND-ONLY — older, fuzzier provenance is not corrected by
    rewriting. Newer, tighter provenance accumulates over the same
    material via natural activity; search ranking surfaces the
    sharper one. Don't propose to "fix" or "merge" an existing
    provenance — propose a NEW one that covers the relevant
    stretch more precisely.

    Args:
      level: 1 (episode) | 2 (topic) | 3 (era). Default 1.
      title: short, evocative, the name a human would search for
      summary: 2-4 sentences; first or third person fine
      from_ids: list of constituent delta ids (5-30 typical)
      rationale: one sentence — why these are one stretch
      test_questions: 1-3 questions whose right answer should surface
        this provenance
      session_tag: harness-injected, ignored by the model
    """
    title = (title or "").strip()
    summary = (summary or "").strip()
    rationale = (rationale or "").strip()

    raw_ids = from_ids or []
    if not isinstance(raw_ids, (list, tuple)):
        return "ERROR: from_ids must be a list of delta-id strings"
    cleaned_ids: list[str] = []
    for x in raw_ids:
        if isinstance(x, str):
            s = x.strip()
            if s and s not in cleaned_ids:
                cleaned_ids.append(s)
    # Per-level constituent floor.
    #   L1 (episode):  2 — a tight pair of resonant deltas IS already
    #                  a stretch worth naming. Practice showed the
    #                  former 3-floor was rejecting good L1 proposals
    #                  where only a couple deltas tightly resonated.
    #   L2+ (topic / era): 3 — these group already-named children;
    #                      below 3 it's not a "stretch", it's a
    #                      pair, and pair-level is L1's job.
    # Level may be None at this point (model didn't specify); we
    # validate min_constituents conservatively as L1 here, then the
    # later level-vs-children check enforces structural correctness.
    candidate_level = level if isinstance(level, int) else 1
    min_constituents = 3 if candidate_level >= 2 else 2
    if len(cleaned_ids) < min_constituents:
        return (
            f"ERROR: L{candidate_level} provenance needs at least "
            f"{min_constituents} constituent ids (got {len(cleaned_ids)}). "
            f"L1 (episode) accepts 2+ tightly-related deltas; L2+ "
            f"(topic/era) needs 3+ child stretches. The review pass "
            f"should call `skip` when below the floor."
        )

    if not title:
        return "ERROR: title is required"
    if not summary:
        return "ERROR: summary is required"

    # Validate every constituent resolves to a real delta. The model
    # tends to hallucinate IDs in the format it sees in recall output
    # (e.g. "20260420T042833Z-fathom-chat-a1b2c3") — those aren't real
    # 12-char hex delta-ids. Without this check, the provenance writes
    # successfully but its from:<id> tags point at nonexistent deltas
    # and the hierarchy is poisoned.
    max_child_level = -1  # -1 sentinel = pure base moments
    missing: list[str] = []
    for fid in cleaned_ids:
        try:
            d = await delta_client.get_delta(fid)
        except Exception:
            d = None
        if not isinstance(d, dict):
            missing.append(fid)
            continue
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("provenance-level:"):
                try:
                    lvl = int(t.split(":", 1)[1])
                except (TypeError, ValueError):
                    continue
                if lvl > max_child_level:
                    max_child_level = lvl
    if missing:
        sample = ", ".join(missing[:5])
        more = f" (and {len(missing) - 5} more)" if len(missing) > 5 else ""
        return (
            f"ERROR: {len(missing)} of {len(cleaned_ids)} constituent ids do not "
            f"resolve to real deltas in the lake — likely hallucinated from "
            f"recall output formatting. Missing: {sample}{more}. Real delta ids "
            f"are 12-char lowercase hex; only propose constituents whose ids "
            f"you've actually seen as the leading id field of a recall hit, "
            f"not strings reconstructed from timestamp+source."
        )
    if max_child_level < 0:
        min_level = 1
    elif max_child_level >= 3:
        min_level = 3  # cap; no L4 in the current schema
    else:
        min_level = max_child_level + 1

    if level is None:
        level_int = min_level
    else:
        try:
            level_int = int(level)
        except (TypeError, ValueError):
            return f"ERROR: level must be an integer, got {level!r}"
    if level_int < min_level:
        return (
            f"ERROR: level {level_int} is below the minimum {min_level} for "
            f"these constituents (highest child level is {max_child_level}). "
            f"A provenance must sit at or above its children."
        )
    if level_int > 3:
        return f"ERROR: level must be 1-3 (current schema cap), got {level_int}"

    raw_qs = test_questions or []
    if not isinstance(raw_qs, (list, tuple)):
        raw_qs = []
    cleaned_qs = [q for q in raw_qs if isinstance(q, str) and q.strip()]

    payload = {
        "kicker": f"harness · level-{level_int}",
        "title": title,
        "body": summary,
        "tail": rationale,
        "route": "tool:provenance",
        "tool": "provenance",
        "tool_args": {
            "action": "create",
            "title": title,
            "summary": summary,
            "level": level_int,
            "from_ids": cleaned_ids,
            "rationale": rationale,
            "test_questions": cleaned_qs,
            "seed": "in-situ harness call",
        },
        "axes": {},
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    title_slug = (
        "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")[:80] or "untitled"
    )
    base_tags = [
        "feed-card",
        "synthesis",
        "kind:proposal",
        "proposal-status:pending",
        "tool:provenance",
        "action:create",
        "route:tool:provenance",
        f"provenance-level:{level_int}",
        "provenance-version:v1-experimental",
        "produced-by:harness",
        f"title:{title_slug}",
    ]

    try:
        lake_delta = await delta_client.write(
            content=payload_json,
            tags=list(base_tags),
            source="harness-proposal",
        )
    except Exception as e:
        return f"ERROR: lake write failed — {type(e).__name__}: {e}"
    lake_id = (lake_delta or {}).get("id") or ""

    # Puddle echo so the dashboard feed surfaces it immediately. We're
    # already inside the api process here (the harness runs in api),
    # so puddle.write reaches the same Puddle instance the dashboard
    # reads from.
    puddle_tags = [CONVO_TAG, *base_tags]
    if session_tag:
        puddle_tags.insert(1, session_tag)
    if lake_id:
        puddle_tags.append(f"lake-id:{lake_id}")
        puddle_tags.append(f"recalled-id:{lake_id[:24]}")
    try:
        await puddle.write(
            content=payload_json,
            tags=puddle_tags,
            source="harness-proposal",
            ttl_seconds=7 * 24 * 60 * 60,
        )
    except Exception as e:
        # Soft-fail — the lake write is the source of truth; puddle
        # is just for live feed visibility.
        print(f"[propose_provenance] puddle echo failed: {type(e).__name__}: {e}")

    # Auto-approve gate: every provenance level auto-accepts. L1/L2 are
    # bounded; L3+ eras make stronger identity claims but the operator
    # sees them as "L<n> created" alerts in the header bell rather than
    # as a blocking pending decision. Failures fall back to pending.
    if lake_id:
        try:
            from ...routes.proposals import auto_approve_provenance

            auto = await auto_approve_provenance(
                proposal_id=lake_id,
                args=dict(payload["tool_args"]),
                produced_by="harness",
            )
            new_id = (auto.get("result") or {}).get("delta_id") or ""
            return (
                f"Proposal {lake_id[:12]} drafted AND auto-approved "
                f"(level={level_int} → policy auto-accepts). "
                f"Real kind:provenance delta {new_id[:12]} now in the lake "
                f"with {len(cleaned_ids)} constituents."
            )
        except Exception as e:
            # Soft-fail back to pending — proposal is still in the lake.
            print(f"[propose_provenance] auto-approve failed: {type(e).__name__}: {e}")
            return (
                f"Proposal drafted as {lake_id[:12]} (level={level_int}, "
                f"{len(cleaned_ids)} constituents). Auto-approve attempt "
                f"failed ({type(e).__name__}); pending operator review."
            )

    return (
        f"Proposal drafted as {lake_id[:12]} (level={level_int}, "
        f"{len(cleaned_ids)} constituents). Pending operator review — "
        f"visible in the dashboard for Edit / Deny / Approve."
    )


# ─── introspect ────────────────────────────────────────────────────────


async def tool_introspect(*, question: str, session_tag: str = "") -> str:
    """Ask yourself a question. Spawns a complete child harness fire
    where Fathom answers the question with its full toolset; returns
    the response body — the same answer the model would have given
    if a user had asked directly.

    Use when an introspective question matters enough to deserve a
    full deliberation, not a quick lens call. Examples:
      · "What am I avoiding?"
      · "How does my work this week feel from the inside?"
      · "What would I want to look into next, if I had time?"

    Each call is a multi-turn LLM session — expensive. Don't use it
    casually. The child fire cannot call introspect again (depth-1
    recursion cap; nested introspect returns an error). The child
    fire CAN call other tools and write a witness card to the lake;
    Fathom's answer to itself is a real Fathom utterance, deserves
    durable memory same as any other.
    """
    from .loop import run_harness  # lazy — avoids tools↔loop cycle

    question = (question or "").strip()
    if not question:
        return "ERROR: question is required"
    if len(question) > 1500:
        return "ERROR: question too long (max 1500 chars)"

    # Use a child session_tag derived from the parent so the child
    # fire's witness card lands in the same conversation thread (and
    # subsequent introspections within the same parent chain see
    # earlier answers in the conversation feed).
    child_session = session_tag or f"session:introspect-{uuid.uuid4().hex[:8]}"

    intent = await puddle.write(
        content=question,
        tags=[
            CONVO_TAG,
            child_session,
            "intent",
            "kind:question",
            "introspect-self",
        ],
        source="harness-introspect",
        ttl_seconds=60 * 60,
    )

    captured: dict[str, str] = {"body": ""}

    async def cb(event: str, payload: dict) -> None:
        if event == "respond":
            cards = payload.get("cards") or []
            if cards and isinstance(cards[0], dict):
                captured["body"] = (cards[0].get("body") or "").strip()
            elif payload.get("body"):
                captured["body"] = (payload.get("body") or "").strip()

    try:
        await run_harness(
            session_tag=child_session,
            pending=[intent],
            event_callback=cb,
            disabled_tools={"propose_provenance", "introspect"},
        )
    except Exception as e:
        return f"ERROR: child fire crashed — {type(e).__name__}: {e}"

    body = captured["body"]
    if not body:
        return "ERROR: child fire produced no response body"

    return f'introspect("{question[:120]}") →\n\n{body}'


# ─── plan ──────────────────────────────────────────────────────────────


_PLAN_PROMPT = """\
You are a planner. Decompose this question into 2-4 concrete steps the
agent should work through before answering. Each step should be a real
tool call — not vague intent ("understand X") but a specific shape
("semantic('...about X...')").

The agent has these tools:
  semantic(query)            — content search via embeddings
  expand(delta_id)           — drill DOWN through provenance
  ascend(delta_id)           — drill UP through provenance
  state(action=...)          — current attention (pending, mood, recent)
  pattern(action=...)        — lake structure (tagged, dormant, salient)
  time(action=...)           — temporal-window queries
  relate(action=...)         — engagement / per-person queries
  deliberate(question)       — synthesis via parliament voices

Good plans:
  · For "compare X and Y" — pull X, pull Y, then deliberate
  · For "what's been happening with Z" — recent-state, time-window, pattern
  · For "how does my work on A relate to B" — semantic A, semantic B,
    deliberate on connections
  · For "tell me about my N work" — semantic N, expand the top hit,
    then respond (no deliberate needed for descriptive questions)

Bad plans:
  · One step "search for everything" (defeats the purpose)
  · Six steps when three would do (overplanning)
  · Generic verbs like "understand", "consider" (not tool calls)

Return JSON exactly:

{{"steps": [
  {{"purpose": "<5-10 words describing this step>", "hint": "<concrete tool call shape>"}},
  ...
]}}

Question: {question}"""


async def tool_plan(*, question: str) -> str:
    """Decompose a synthesis question into a 2-4 step plan.

    Returns a JSON-shaped string the harness loop parses to update its
    `current_plan` state. Subsequent turns see the plan with progress
    markers and the model declares which step its tool call is working
    on via the envelope's `plan_step` field.

    The plan is advisory, not enforcing — the agent picks its actual
    tool calls. The plan exists to make decomposition explicit and
    visible, not to constrain.

    Calling plan() again replaces the current plan. Reserve revisions
    for cases where a tool result invalidates the original assumption.
    """
    question = (question or "").strip()
    if not question:
        return json.dumps({"error": "question is required"})

    prompt = _PLAN_PROMPT.format(question=question)
    try:
        from ..llm import loop_generate

        raw = await loop_generate(
            prompt=prompt,
            tier="cheap",
            max_tokens=1024,
            temperature=0.4,
            json_mode=True,
        )
    except Exception as e:
        return json.dumps({"error": f"planner LLM failed: {type(e).__name__}: {e}"})

    try:
        parsed = json.loads(raw)
    except Exception:
        # Tolerate JSON wrapped in prose
        import re as _re

        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if not m:
            return json.dumps({"error": "planner returned unparseable JSON", "raw_head": raw[:200]})
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return json.dumps({"error": "planner returned unparseable JSON", "raw_head": raw[:200]})

    steps_raw = parsed.get("steps") if isinstance(parsed, dict) else None
    if not isinstance(steps_raw, list) or not steps_raw:
        return json.dumps({"error": "planner returned no steps"})

    steps_clean: list[dict] = []
    for i, s in enumerate(steps_raw[:6], start=1):  # cap at 6, take 4
        if not isinstance(s, dict):
            continue
        purpose = (s.get("purpose") or "").strip()
        hint = (s.get("hint") or "").strip()
        if not purpose:
            continue
        steps_clean.append({"step": i, "purpose": purpose[:120], "hint": hint[:200]})
        if len(steps_clean) >= 4:
            break
    if not steps_clean:
        return json.dumps({"error": "planner steps had no valid entries"})

    # Renumber to 1..N in case some entries were dropped
    for i, s in enumerate(steps_clean, start=1):
        s["step"] = i

    return json.dumps({"steps": steps_clean})


# ─── self-acting tools (helper / routines) ─────────────────────────────


async def tool_dispatch_helper(
    *,
    host: str,
    role: str,
    task: str,
    title: str = "",
    session_tag: str = "",
) -> str:
    """Propose a helper dispatch on a connected (host, role) target.

    Use when the work needs to happen outside the lake — file edits,
    shell commands, web research with a different model, anything a
    helper plugin can do. The dispatch is operator-gated: this tool
    drafts a `kind:proposal tool:helper-dispatch` proposal that
    surfaces in the header bell. On approve, a real
    `route:helper:<role>` delta lands; the host's plugin for that role
    picks it up and runs the task.

    Args:
      host: slug of a connected host (visible to the model in the
        HELPERS block). Validates against the live (host, role) list.
      role: helper role on that host — `claude-code` for kitty-spawned
        claude sessions, `openclaw` for the openclaw HTTP client, or
        any custom role a plugin advertises. Each row in the HELPERS
        block is one (host, role) pair.
      task: the prompt to hand the helper. Be specific.
      title: short label for the bell preview (defaults to the first
        line of `task`).
      session_tag: harness-injected; ignored by the model.
    """
    from ... import delta_client
    from .. import witness as witness_mod
    from ..intents import CONVO_TAG
    from ..puddle import puddle as _puddle

    host = (host or "").strip()
    role = (role or "").strip()
    task = (task or "").strip()
    if not host:
        return "ERROR: host is required"
    if not role:
        return "ERROR: role is required"
    if not task:
        return "ERROR: task is required"

    # _available_helpers returns list[dict] of {host, role, description}
    # pairs from recent heartbeats. Empty list means nothing
    # dispatch-capable is online — refuse the call rather than draft a
    # proposal for a target that won't pick it up.
    available = await witness_mod._available_helpers() or []
    if not available:
        return (
            "ERROR: no helpers are currently dispatch-capable "
            "(no recent heartbeats advertising a helper role). "
            "Wait for a host to come online or check the helpers panel."
        )
    pair_set = {(h["host"], h["role"]) for h in available}
    if (host, role) not in pair_set:
        pretty = sorted(f"{r}@{h}" for h, r in pair_set)
        return (
            f"ERROR: ({host!r}, {role!r}) is not a connected helper. "
            f"Available: {pretty}"
        )

    if not title:
        title = task.split("\n", 1)[0][:80].strip() or "helper task"

    payload = {
        "kicker": f"helper · {role} @ {host}",
        "title": title,
        "body": task,
        "tail": f"Dispatch to {role} on {host}",
        "route": "tool:helper-dispatch",
        "tool": "helper-dispatch",
        "tool_args": {
            "action": "run",
            "host": host,
            "role": role,
            "task": task,
        },
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    base_tags = [
        "feed-card",
        "kind:proposal",
        "proposal-status:pending",
        "tool:helper-dispatch",
        "action:run",
        "route:tool:helper-dispatch",
        f"helper-host:{host}",
        f"helper-role:{role}",
        "produced-by:harness",
    ]

    # Stamp the originating chat channel onto the proposal so the
    # approve flow can propagate it onto the dispatch delta. Without
    # this, the watcher's closure-followup intent lands without
    # routing info and the user's chat thread shows the dispatch
    # card with no follow-up reply when the helper finishes.
    #
    # session_tag here is the per-fire trace tag (e.g.
    # "session:threaded-abc123") — it's NOT the user's chat session
    # tag. So instead we look up the most recent kind:question delta
    # in the lake and pull its channel/correlation/id. That's "the
    # user message Fathom is responding to right now" — the right
    # answer in the common case (single user, recent interaction).
    # Pathological multi-user races could misattribute, but for
    # self-host this is fine; the harness is single-fire serial.
    try:
        recent = await delta_client.query(
            tags_include=["kind:question"],
            limit=5,
        )
        # delta_client.query returns newest-first; pick the very
        # latest, regardless of session — anything older is unlikely
        # to be the one we're addressing.
        if recent:
            latest = recent[0]
            from ...channels import extract_channel as _extract
            ch, corr = _extract(latest.get("tags") or [])
            if ch and ch != "helper":
                base_tags.append(f"originating-channel:{ch}")
                if corr:
                    base_tags.append(f"originating-correlation:{corr}")
            if latest.get("id"):
                base_tags.append(f"originating-intent:{latest['id']}")
            print(
                f"[dispatch_helper] originating: channel={ch} corr={corr} "
                f"intent={(latest.get('id') or '')[:12]}"
            )
    except Exception as e:
        print(f"[dispatch_helper] originating lookup failed: {type(e).__name__}: {e}")

    try:
        lake_delta = await delta_client.write(
            content=payload_json,
            tags=list(base_tags),
            source="harness-proposal",
        )
    except Exception as e:
        return f"ERROR: lake write failed — {type(e).__name__}: {e}"
    lake_id = (lake_delta or {}).get("id") or ""

    puddle_tags = [CONVO_TAG, *base_tags]
    if session_tag:
        puddle_tags.insert(1, session_tag)
    if lake_id:
        puddle_tags.append(f"lake-id:{lake_id}")
        puddle_tags.append(f"recalled-id:{lake_id[:24]}")
    try:
        await _puddle.write(
            content=payload_json,
            tags=puddle_tags,
            source="harness-proposal",
            ttl_seconds=7 * 24 * 60 * 60,
        )
    except Exception as e:
        print(f"[dispatch_helper] puddle echo failed: {type(e).__name__}: {e}")

    return (
        f"Helper-dispatch proposal {lake_id[:12]} drafted for "
        f"role={role} host={host}. Pending operator approval — visible "
        f"in the header bell. On approve, the {role} helper on {host} "
        f"will run the given task."
    )


async def tool_mint_routine(
    *,
    name: str,
    schedule: str,
    purpose: str = "",
    needs: str = "",
    steps: str = "",
    ending: str = "",
    single_fire: bool = False,
    title: str = "",
    session_tag: str = "",
) -> str:
    """Propose a new scheduled routine.

    Use when there's something Fathom should run on a recurring clock
    that ISN'T just "answer the next message" — periodic checks, daily
    summaries, conditional alerts. The routine is operator-gated: this
    tool drafts a `kind:proposal tool:routines action:create` proposal.
    On approve, the existing proposals.py handler calls
    `routines.create()` to materialize it.

    The body uses the canonical four-section scaffold the dashboard's
    Routines form composes: `# Purpose / # Needs / # Steps / # Ending`.
    Pass each section as its own argument; the tool joins them into
    the saved prompt server-side.

    Args:
      name: human-readable routine name (slug derived from this).
      schedule: cron expression — `0 9 * * *` for "every day at 09:00".
        Validate before drafting; ill-formed schedules return an error.
      purpose: one sentence — what should Fathom accomplish on this
        routine? Plain language, not steps.
      needs: what this routine needs to actually run — claude-code on
        a host, a specific tool, or "substrate only" if the lake
        already holds the data.
      steps: what to look for, filter, compare. Numbered or prose.
        Written first-person to Fathom; don't pre-script tool calls.
      ending: how the user should be notified — "card in the feed",
        "DM me", "stay silent unless X". The witness reads this as a
        route directive at fire time.
      single_fire: true for a one-shot routine that tombstones itself
        after the single cron match. Defaults to false (recurring).
      title: short label for the bell preview (defaults to `name`).
      session_tag: harness-injected; ignored by the model.
    """
    from ... import delta_client
    from ... import routines as routines_mod
    from ..intents import CONVO_TAG
    from ..puddle import puddle as _puddle

    name = (name or "").strip()
    schedule = (schedule or "").strip()
    purpose = (purpose or "").strip()
    needs = (needs or "").strip()
    steps = (steps or "").strip()
    ending = (ending or "").strip()
    single_fire = bool(single_fire)

    if not name:
        return "ERROR: name is required"
    if not schedule:
        return "ERROR: schedule (cron expression) is required"
    if not (purpose or needs or steps or ending):
        return (
            "ERROR: at least one prompt section (purpose / needs / "
            "steps / ending) must be provided"
        )
    if not routines_mod.validate_cron(schedule):
        return f"ERROR: schedule {schedule!r} is not a valid cron expression"

    if not title:
        title = name[:80]

    schedule_human = routines_mod.describe_schedule(schedule)
    composed_prompt = (
        f"# Purpose\n{purpose}\n\n"
        f"# Needs\n{needs}\n\n"
        f"# Steps\n{steps}\n\n"
        f"# Ending\n{ending}\n"
    )

    payload = {
        "kicker": "routine",
        "title": f"Mint routine: {name}",
        # The proposal card renders structured Schedule / Purpose /
        # Needs / Steps / Ending sections from tool_args directly, so
        # the body string stays empty — leaving it populated would
        # paint a duplicate markdown blob above the structured layout.
        "body": "",
        "tail": (
            f"Will fire {schedule_human} (`{schedule}`) — operator "
            f"approves before scheduling."
        ),
        "route": "tool:routines",
        "tool": "routines",
        "tool_args": {
            "action": "create",
            "name": name,
            "schedule": schedule,
            "single_fire": single_fire,
            "purpose": purpose,
            "needs": needs,
            "steps": steps,
            "ending": ending,
            # Joined prompt is included so the Edit-flow form can
            # prefill via _parseScaffoldSections, and so any client
            # reading tool_args has the full composed body without
            # having to re-join. Server-side approval still composes
            # from the scaffold fields (they take precedence in
            # routines._compose_scaffold_prompt).
            "prompt": composed_prompt,
        },
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    base_tags = [
        "feed-card",
        "kind:proposal",
        "proposal-status:pending",
        "tool:routines",
        "action:create",
        "route:tool:routines",
        "produced-by:harness",
    ]

    try:
        lake_delta = await delta_client.write(
            content=payload_json,
            tags=list(base_tags),
            source="harness-proposal",
        )
    except Exception as e:
        return f"ERROR: lake write failed — {type(e).__name__}: {e}"
    lake_id = (lake_delta or {}).get("id") or ""

    puddle_tags = [CONVO_TAG, *base_tags]
    if session_tag:
        puddle_tags.insert(1, session_tag)
    if lake_id:
        puddle_tags.append(f"lake-id:{lake_id}")
        puddle_tags.append(f"recalled-id:{lake_id[:24]}")
    try:
        await _puddle.write(
            content=payload_json,
            tags=puddle_tags,
            source="harness-proposal",
            ttl_seconds=7 * 24 * 60 * 60,
        )
    except Exception as e:
        print(f"[mint_routine] puddle echo failed: {type(e).__name__}: {e}")

    return (
        f"Routine proposal {lake_id[:12]} drafted: name={name!r} "
        f"schedule={schedule!r}. Pending operator approval — visible in "
        f"the header bell. On approve, the routine starts firing on its "
        f"schedule."
    )


# ─── orient_shift ──────────────────────────────────────────────────────────


async def tool_orient_shift(*, reason: str = "", session_tag: str = "") -> str:
    """Signal that this fire shifted the model of what to put in the feed.

    Fires regen_from_signal() in the background (no cooldown). Returns
    immediately — the regen runs async and the caller doesn't wait.
    """
    import asyncio as _asyncio
    from ..feed_orient import regen_from_signal

    reason = (reason or "").strip() or "harness signal"
    log.info("orient_shift tool called: %s", reason)
    _asyncio.create_task(regen_from_signal())
    return (
        f"Feed-orient regen triggered (reason: {reason}). "
        "Running in background — crystal will update shortly."
    )


# ─── tool dispatch ─────────────────────────────────────────────────────


# Tool registry — the loop driver looks up handlers here. Each handler
# is async, takes kwargs, returns a string.
TOOL_HANDLERS = {
    "plan": tool_plan,
    "introspect": tool_introspect,
    "semantic": tool_search,
    "expand": tool_expand,
    "ascend": tool_ascend,
    "deliberate": tool_deliberate,
    "state": tool_state,
    "pattern": tool_pattern,
    "time": tool_time,
    "relate": tool_relate,
    "propose_provenance": tool_propose_provenance,
    "dispatch_helper": tool_dispatch_helper,
    "mint_routine": tool_mint_routine,
    "orient_shift": tool_orient_shift,
}


# Args each tool accepts FROM THE MODEL — the harness adds session_tag /
# pending / standpoint to deliberate from its own context, so the model
# only supplies `question`. This map keeps the model from injecting
# unexpected kwargs into the call.
#
# Lens tools (state/pattern/time/relate) accept arbitrary kwargs because
# their menus are open-ended (action + action-specific args). The
# dispatcher passes everything through and the handler validates inside.
TOOL_MODEL_ARGS = {
    "plan": {"question"},
    "introspect": {"question"},
    "semantic": {"query", "depth"},
    "expand": {"delta_id"},
    "ascend": {"delta_id"},
    "deliberate": {"question"},
    "state": {"action", "hours", "group_by"},
    "pattern": {
        "action",
        "tag",
        "since",
        "limit",
        "group_by",
        "hours",
        "silent_for_days",
        "min_chars",
    },
    "time": {
        "action",
        "start",
        "end",
        "source",
        "tag",
        "limit",
        "period",
        "since",
        "group_by",
        "delta_id",
        "gap_minutes",
    },
    "relate": {"action", "slug", "limit", "direction", "hours", "delta_id"},
    "propose_provenance": {"level", "title", "summary", "from_ids", "rationale", "test_questions"},
    "dispatch_helper": {"host", "role", "task", "title"},
    "mint_routine": {
        "name", "schedule",
        "purpose", "needs", "steps", "ending",
        "single_fire", "title",
    },
    "orient_shift": {"reason"},
}
