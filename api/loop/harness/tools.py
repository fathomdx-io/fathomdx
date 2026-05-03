"""Harness tools — what Fathom can call from inside the agentic loop.

Two tiers:

  First tier (always visible to the model):
    search       — semantic recall via the LLM-composed plan executor
    expand       — graph traversal: fetch children of a provenance delta
    ascend       — graph traversal: find provenance that contains a delta
    deliberate   — synthesis: parliament voices on a question
    state        — current attention: intents, proposals, mood, crystal,
                   recent activity. Call state(action='help') to discover.
    pattern      — aggregations + lake-wide analysis: tag filters, counts,
                   salience rankings, dormant signals. Call
                   pattern(action='help').
    time         — temporal-window queries: between dates, group-by-day.
                   Call time(action='help').
    relate       — engagement / relational: who, what's been affirmed,
                   what's been dropped. Call relate(action='help').

  Second tier (sub-actions on the lens tools above): each lens has a
  small menu of structured queries. Their return shapes always include
  delta ids the model can feed back into `expand`/`ascend`/`search` —
  the lenses surface, the first-tier tools navigate.

`deliberate` wraps the existing convener + parliament one-round path; it
does NOT reimplement deliberation. The harness inverts the relationship —
deliberation is now elective, called from within the harness when the
question calls for antagonism, instead of being a mandatory pre-step.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import uuid

from .. import resonance  # noqa: F401  — process imports it transitively
from ... import delta_client
from ... import search as search_mod
from ..convener import run_convener
from ..intents import CONVO_TAG, intent_kind, pending_intents
from ..process import run_process
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
    for tag in (delta.get("tags") or []):
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
    session_tag: str,
    pending: list[dict],
    standpoint: Any,
) -> str:
    """Spin up parliament voices on a question and return their thoughts.

    Wraps the existing convener + one round of parallel voices. Voice
    thought deltas land in the puddle under `session_tag` so subsequent
    harness turns can also see them via resonance, but the immediate
    return is a rendered text block of the takes.

    `pending` and `standpoint` are the harness's outer-loop snapshot —
    convener reads them as constraint context. The `question` argument
    is what the harness wants the parliament to think about specifically;
    it gets surfaced in the convener's intent_block alongside the outer
    pending intents so depth/voice picks reflect this narrower focus.
    """
    question = (question or "").strip()
    if not question:
        return "ERROR: empty deliberation question"

    # Synthesize a minimal intent dict the convener can read. Not written
    # to the puddle — this is just an in-memory shape for the convener's
    # _render_intent_block helper.
    deliberation_intent = {
        "id": f"deliberate-{uuid.uuid4().hex[:12]}",
        "content": question,
        "tags": [f"kind:deliberation", "harness:deliberate"],
        "source": "harness",
    }
    convener_pending = [deliberation_intent] + (pending or [])

    try:
        verdict = await run_convener(
            session_tag=session_tag,
            pending=convener_pending,
            standpoint=standpoint,
        )
    except Exception as e:
        return f"ERROR: convener crashed — {type(e).__name__}: {e}"

    if verdict.depth == "zero" or not verdict.voices:
        return (
            f"(convener picked depth=zero — voices declined to weigh in. "
            f"Rationale: {verdict.rationale or 'no rationale given'})"
        )

    voice_coros = [
        run_process(
            pid=f"harness-{v['name']}-{uuid.uuid4().hex[:6]}",
            session_tag=session_tag,
            voice=v,
            pending=convener_pending,
            peer_voices=verdict.voices,
            standpoint=standpoint,
        )
        for v in verdict.voices
    ]
    try:
        results = await asyncio.gather(*voice_coros, return_exceptions=True)
    except Exception as e:
        return f"ERROR: parliament crashed — {type(e).__name__}: {e}"

    blocks: list[str] = [
        f"Parliament — depth={verdict.depth}, voices=[{', '.join(v['name'] for v in verdict.voices)}]",
        f"Rationale: {verdict.rationale or '(no rationale)'}",
        "",
    ]
    for v, res in zip(verdict.voices, results):
        if isinstance(res, Exception):
            blocks.append(f"VOICE: {v['name'].upper()} — crashed ({type(res).__name__}: {res})")
            blocks.append("")
            continue
        text = (res or "").strip() or "(silence)"
        blocks.append(f"VOICE: {v['name'].upper()}")
        blocks.append(f"  {text}")
        blocks.append("")
    return "\n".join(blocks).rstrip()


# ─── shared helpers ────────────────────────────────────────────────────


def _render_delta_brief(d: dict) -> str:
    """One-block rendering of a delta — id prefix, source, ts, content snippet.

    Used by `expand` and `ascend` so their results read consistently.
    """
    did = (d.get("id") or "")[:12] or "?"
    src = d.get("source") or "lake"
    ts = (d.get("timestamp") or d.get("created_at") or "")[:16]
    content = (d.get("content") or "").strip().replace("\n", " ")
    if len(content) > 320:
        content = content[:320] + "…"
    tag_summary = ""
    tags = d.get("tags") or []
    kind_tags = [t for t in tags if isinstance(t, str) and t.startswith("kind:")]
    if kind_tags:
        tag_summary = f" [{', '.join(kind_tags)}]"
    return f"  · {did} {src} {ts}{tag_summary}\n      {content}"


# ─── lens tools: state / pattern / time / relate ──────────────────────


_LENS_RESULT_LIMIT = 30          # cap items returned per lens call


def _short(d: dict, *, max_chars: int = 240) -> str:
    """Compact one-line render of a delta — what every lens uses to
    show its results. id prefix · source · ts · (tags) · content snippet."""
    did = (d.get("id") or "")[:12] or "?"
    src = (d.get("source") or "lake")[:24]
    ts = (d.get("timestamp") or d.get("created_at") or "")[:19]
    content = (d.get("content") or "").strip().replace("\n", " ")
    if len(content) > max_chars:
        content = content[:max_chars] + "…"
    tags = d.get("tags") or []
    salient = []
    for t in tags:
        if not isinstance(t, str):
            continue
        if t.startswith("kind:") or t.startswith("provenance-level:"):
            salient.append(t)
        if len(salient) >= 3:
            break
    tag_part = f" [{', '.join(salient)}]" if salient else ""
    return f"  · {did} {src} {ts}{tag_part}\n      {content}"


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
            content = (it.get("content") or "").strip().split(
                "\n\n[intent-payload]", 1
            )[0]
            content = content.replace("\n", " ")
            if len(content) > 280:
                content = content[:280] + "…"
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
            title = payload.get("title") or (d.get("content") or "")[:80]
            did = (d.get("id") or "")[:12]
            ts = (d.get("timestamp") or "")[:19]
            blocks.append(f"  · {did} tool={tool} {ts}\n      {title}")
        return "\n".join(blocks)

    if action == "mood":
        try:
            items = puddle.query(tags_include=[CONVO_TAG, "mood"], limit=8)
        except Exception as e:
            return f"ERROR: mood query failed — {type(e).__name__}: {e}"
        if not items:
            # Fallback to lake — telepathy may not have mirrored mood
            # if it's offline (e.g. FATHOM_QUIET_MODE).
            try:
                items = await delta_client.query(
                    tags_include=["kind:mood"],
                    limit=8,
                )
            except Exception:
                items = []
        if not items:
            return "(no mood deltas surfaced — substrate may be empty)"
        blocks = [f"Mood ({len(items)}):", ""]
        for d in items[:8]:
            blocks.append(_short(d, max_chars=280))
        return "\n".join(blocks)

    if action == "crystal":
        try:
            items = await delta_client.query(
                tags_include=["identity-crystal"],
                limit=8,
            )
        except Exception as e:
            return f"ERROR: crystal query failed — {type(e).__name__}: {e}"
        if not items:
            return "(no identity-crystal deltas in the lake)"
        blocks = [f"Identity crystal facets ({len(items)}):", ""]
        for d in items[:6]:
            blocks.append(_short(d, max_chars=400))
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
                f"Activity in last {hours}h ({len(items)} deltas, "
                f"{len(counts)} sources):",
                "",
            ]
            for src, n in counts.most_common(20):
                # Show 1-2 sample contents per source.
                samples = [d for d in items if d.get("source") == src][:2]
                blocks.append(f"  ── {src} · {n} deltas ──")
                for d in samples:
                    content = (d.get("content") or "").strip().replace("\n", " ")
                    if len(content) > 120:
                        content = content[:120] + "…"
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
                tags_include=[], time_start=since, limit=2000,
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
                    try:
                        axes_by_card[cid] = json.loads(ad.get("content") or "{}")
                    except Exception:
                        pass
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
            title = (payload.get("title") or payload.get("body") or "")[:120]
            did = (c.get("id") or "")[:12]
            blocks.append(
                f"  · {did} score={score:.2f} "
                f"(s={ax.get('salience',0):.2f} r={ax.get('resonance',0):.2f} "
                f"c={ax.get('confidence',0):.2f})\n      {title}"
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
                tags_include=[], limit=300,
            )
        except Exception as e:
            return f"ERROR: dormant query failed — {type(e).__name__}: {e}"
        old = [
            d for d in items
            if (d.get("timestamp") or "") < cutoff
            and len((d.get("content") or "")) >= min_chars
        ]
        if not old:
            return (
                f"(nothing dormant — no substantive deltas older than "
                f"{silent_days} days surfaced in the recent slice. The lake "
                f"reaper may have culled them, or this query needs a wider net.)"
            )
        old.sort(key=lambda d: d.get("timestamp") or "")
        blocks = [
            f"Dormant deltas (older than {silent_days}d, "
            f">={min_chars} chars, {len(old)} found):",
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

Returned items include delta ids — feed them into expand/ascend/search."""


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
        items = [
            d for d in (items or [])
            if (d.get("timestamp") or "") <= end
        ]
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
                tags_include=[], time_start=since, limit=3000,
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
                    dt = datetime.fromisoformat(
                        (ts[:-1] + "+00:00") if ts.endswith("Z") else ts
                    )
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
                tags_include=[f"contact:{slug}"], limit=limit,
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
            blocks.append(
                f"  · {attest_id[:12]} → {target_id[:12]}"
            )
        return "\n".join(blocks)

    if action == "dropped_around":
        target = (kwargs.get("delta_id") or "").strip()
        if not target:
            return "ERROR: relate.dropped_around requires `delta_id`"
        try:
            items = await delta_client.query(
                tags_include=[f"refutes:{target}"], limit=_LENS_RESULT_LIMIT,
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
                    tags_include=[tag], limit=_LENS_RESULT_LIMIT,
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


# ─── tool dispatch ─────────────────────────────────────────────────────


# Tool registry — the loop driver looks up handlers here. Each handler
# is async, takes kwargs, returns a string.
TOOL_HANDLERS = {
    "search":     tool_search,
    "expand":     tool_expand,
    "ascend":     tool_ascend,
    "deliberate": tool_deliberate,
    "state":      tool_state,
    "pattern":    tool_pattern,
    "time":       tool_time,
    "relate":     tool_relate,
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
    "search":     {"query", "depth"},
    "expand":     {"delta_id"},
    "ascend":     {"delta_id"},
    "deliberate": {"question"},
    "state":      {"action", "hours", "group_by"},
    "pattern":    {"action", "tag", "since", "limit", "group_by", "hours",
                   "silent_for_days", "min_chars"},
    "time":       {"action", "start", "end", "source", "tag", "limit",
                   "period", "since", "group_by"},
    "relate":     {"action", "slug", "limit", "direction", "hours", "delta_id"},
}
