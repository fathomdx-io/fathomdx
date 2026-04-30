"""Canonical natural-language recall over the delta lake.

One entry point — ``search(text, depth, ...)`` — returns a hierarchical
structured result. Used by every surface that asks the lake a question:
the web-chat pre-recall layer, the ``POST /v1/search`` endpoint (CLI,
MCP, claude-code recall hook), the loop's intent-searcher and voice-
followup ticks. Every surface shares the same plan, the same DAG
rendering, and the same voice.

Deep mode generates a compositional plan via the planner LLM, executes
it against the delta store, walks the DAG, emits an associative trail,
then synthesizes a ``kind:sediment`` thinking-delta back into the lake
with ``from:<id>`` provenance pointers to each source — turning the act
of recall into sediment formation. Shallow mode runs a single semantic
search wrapped in the same shape so callers don't branch.

Three reranking / expansion layers run on every deep recall:

  * **Noise rerank** lives in the plan executor (over-fetch + demote
    short / generic-noise-centroid-aligned content + trim).
  * **Valence rerank** runs here, after engagement clouds attach —
    refuted deltas sink, affirmed / ``from:``-cited ones float.
  * **Sediment provenance auto-expand** follows ``from:<id>`` pointers
    on any surfaced sediment to bring its cited sources into the trail
    as a synthetic ``_provenance`` step.

Result shape::

    {
      "plan": {...},                    # plan executed (shallow = synthetic)
      "tree": [                         # topo-ordered DAG nodes
        {"id": "a", "relation": "first came to mind",
         "parents": [], "action": "search", "query": "...",
         "delta_ids": ["...", ...]},
        {"id": "b", "relation": "which pulled on",
         "parents": ["a"], "action": "chain", "query": "a",
         "delta_ids": [...]},
      ],
      "deltas_by_step": {"a": [...], "b": [...]},
      "total_count": int,
      "media_hashes": [...],            # up to 5 for UI thumbnails
      "as_prompt": str,                 # pre-rendered hierarchical text
      "thinking_prose": str | None,     # distilled synthesis (deep only)
      "thinking_id": str | None,        # lake id of the sediment delta
    }
"""

from __future__ import annotations

import json
import logging

from . import delta_client
from .prompt import SEARCH_PLANNER_PROMPT

log = logging.getLogger(__name__)

_ACTION_KEYS = (
    "search",
    "filter",
    "chain",
    "bridge",
    "intersect",
    "union",
    "diff",
    "aggregate",
    "neighbors",
    "timeline",
)

_DEFAULT_RELATION_BY_ACTION = {
    "search": "surfaced",
    "filter": "from around that time",
    "chain": "and that reminded me of",
    "bridge": "bridging those to",
    "intersect": "and the overlap",
    "union": "taken together",
    "diff": "but not",
    "aggregate": "grouped",
    "neighbors": "and what was around it",
    "timeline": "the moment around it",
}

# Sources whose bursts get run-length collapsed inside a timeline window.
# Two flavors of noise here:
#   * Heartbeat-shaped telemetry (heartbeat, sysinfo, laptop-health) —
#     ticks on a clock, content is structurally similar each time, and
#     having ten of them in a strip drowns the actual signal.
#   * Loop self-references (witness, fathom-loop, fathom-mood, fathom-feed,
#     fathom-sediment) — when a recall is for the loop's own substrate,
#     sequences of these crowd out the human/lake content the recall
#     was actually about.
# Conversational sources (claude-code, fathom-chat) NEVER go here — each
# of those deltas carries unique narrative content and collapsing would
# erase the moment.
_TIMELINE_COLLAPSE_SOURCES = [
    "agent-heartbeat",
    "fathom-agent",
    "sysinfo",
    "laptop-health",
    "witness",
    "fathom-loop",
    "fathom-mood",
    "fathom-feed",
    "fathom-sediment",
    "homeassistant",
]

# Steps that produce timestamped deltas usable as timeline anchors.
# `aggregate` produces buckets, not deltas; `filter` / set ops can
# produce deltas but typically aren't the load-bearing recall hit.
_TIMELINE_ANCHOR_ACTIONS = {"search", "chain", "bridge", "neighbors"}

_MAX_CONTENT_CHARS = 1200
_MAX_MEDIA_HASHES = 5


# ── Planner (deep mode) ─────────────────────────


async def _generate_plan(
    text: str,
    conv_context: str = "",
    session_slug: str | None = None,
) -> dict | None:
    """Fast LLM call that composes a multi-step plan annotated with relations."""
    prompt = text
    if conv_context:
        prompt = f"Conversation so far:\n{conv_context}\n\nLatest message: {text}"

    try:
        from . import llm_config

        medium_client, medium_model = await llm_config.resolve_tier("medium")
        resp = await medium_client.chat.completions.create(
            model=medium_model,
            messages=[
                {"role": "system", "content": SEARCH_PLANNER_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        plan = json.loads(raw)
    except Exception:
        return None

    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        return None
    if not plan["steps"]:
        return None

    if session_slug:
        _inject_session_step(plan, session_slug)

    return plan


def _inject_session_step(plan: dict, session_slug: str) -> None:
    """Add a session-scoped filter step (medium-term memory) to the plan."""
    session_step = {
        "id": "_session",
        "relation": "and from this conversation",
        "filter": {
            "tags_include": ["fathom-chat", f"chat:{session_slug}"],
        },
        "limit": 30,
    }
    last = plan["steps"][-1]
    if isinstance(last.get("union"), list):
        last["union"].append("_session")
        plan["steps"].insert(-1, session_step)
        return

    ids = [s["id"] for s in plan["steps"]]
    plan["steps"].append(session_step)
    plan["steps"].append(
        {
            "id": "_combined",
            "union": [ids[0], "_session"],
            "relation": "taken together",
        }
    )


# ── Sediment synthesis ──────────────────────────

SEDIMENT_SYNTHESIS_PROMPT = """You are the mind distilling what it just recalled.

You'll get a query and a set of memories that surfaced in response. Your job
is to write what *you* — the mind — conclude from them. Not a summary, not a
list, not "based on the results" framing. Sediment: the compacted take that
would form naturally if this recall repeated many times.

Rules:
- Speak in first person, as the mind. "I remember" is fine. "The results
  show" is not.
- One paragraph, flowing prose. No bullets, no headers.
- If the memories contradict each other, say so — don't flatten them.
- If they converge on something specific, say that directly.
- Surface the load-bearing conclusion, not every detail. Details remain
  in the sources — that's what `from:` pointers are for.
- Em dashes over parentheses. No staccato fragments. No mic-drop closers.
- Under 150 words.
"""

_SEDIMENT_MIN_DELTAS = 2
_SEDIMENT_MAX_SOURCES = 20
_SEDIMENT_PROMPT_CHAR_BUDGET = 6000


def _sediment_source_ids(deltas_by_step: dict[str, list[dict]]) -> list[str]:
    """Unique delta ids across all steps, preserving first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for deltas in deltas_by_step.values():
        for d in deltas:
            did = d.get("id")
            if not did or did in seen:
                continue
            seen.add(did)
            ordered.append(did)
            if len(ordered) >= _SEDIMENT_MAX_SOURCES:
                return ordered
    return ordered


def _sediment_prompt_body(query: str, deltas_by_step: dict[str, list[dict]]) -> str:
    """Compact rendering of the retrieved set for the synthesis LLM call.

    Any engagement cloud attached to a source is rendered as indented notes
    so the synthesizer can see when a memory has been refuted or affirmed
    — and avoid re-deriving a take the mind has already rejected.
    """
    lines: list[str] = [f'Query: "{query}"', "", "Memories that surfaced:"]
    used = 0
    for deltas in deltas_by_step.values():
        for d in deltas:
            content = (d.get("content") or "").strip().replace("\n", " ")
            content = content[:400]
            src = d.get("source") or "unknown"
            ts = (d.get("timestamp") or "")[:10]
            line = f"  • [{src} · {ts}] {content}"
            used += len(line)
            if used > _SEDIMENT_PROMPT_CHAR_BUDGET:
                lines.append("  … (truncated)")
                return "\n".join(lines)
            lines.append(line)
            cloud_note = _render_cloud(d.get("engagement_cloud") or [])
            if cloud_note:
                used += len(cloud_note)
                lines.append(cloud_note.lstrip("\n"))
    return "\n".join(lines)


async def _synthesize_thinking(
    query: str,
    deltas_by_step: dict[str, list[dict]],
) -> tuple[str | None, str | None]:
    """Compose a sediment delta from the retrieved set and write it back.

    Returns (thinking_prose, thinking_id). Returns (None, None) if the
    retrieved set is too thin to synthesize, or if the LLM or write fails
    — recall should not break on sediment failures.
    """
    source_ids = _sediment_source_ids(deltas_by_step)
    if len(source_ids) < _SEDIMENT_MIN_DELTAS:
        return None, None

    body = _sediment_prompt_body(query, deltas_by_step)
    try:
        from . import llm_config

        medium_client, medium_model = await llm_config.resolve_tier("medium")
        resp = await medium_client.chat.completions.create(
            model=medium_model,
            messages=[
                {"role": "system", "content": SEDIMENT_SYNTHESIS_PROMPT},
                {"role": "user", "content": body},
            ],
            temperature=0.3,
        )
        prose = (resp.choices[0].message.content or "").strip()
    except Exception:
        log.exception("search: sediment synthesis LLM call failed")
        return None, None

    if not prose:
        return None, None

    tags = ["kind:sediment"] + [f"from:{sid}" for sid in source_ids]
    try:
        written = await delta_client.write(
            content=prose,
            tags=tags,
            source="fathom-sediment",
        )
    except Exception:
        log.exception("search: sediment write failed")
        return prose, None

    return prose, written.get("id")


# ── DAG inspection ──────────────────────────────


def _action_of(step: dict) -> tuple[str, object]:
    for k in _ACTION_KEYS:
        if k in step:
            return k, step[k]
    return "unknown", None


def _parents_of(step: dict) -> list[str]:
    action, val = _action_of(step)
    if action in ("chain", "aggregate", "neighbors", "timeline"):
        return [val] if isinstance(val, str) else []
    if action in ("bridge", "intersect", "union", "diff"):
        return [v for v in val if isinstance(v, str)] if isinstance(val, list) else []
    return []


def _inject_timeline_step(plan: dict) -> str | None:
    """Append a timeline step that anchors on the last delta-producing step.

    Walks the plan steps in order, picks the last step whose action is in
    `_TIMELINE_ANCHOR_ACTIONS` as the anchor source. Returns the new step
    id, or None if nothing in the plan is a viable anchor source (e.g.
    aggregate-only plan)."""
    steps = plan.get("steps") or []
    anchor_id: str | None = None
    for step in steps:
        action, _ = _action_of(step)
        if action in _TIMELINE_ANCHOR_ACTIONS:
            anchor_id = step["id"]
    if not anchor_id:
        return None
    # Pick a unique step id even if the planner happened to emit one
    # named "_timeline" (defensive — it shouldn't, but the planner is an
    # LLM and we'd rather not let it collide).
    sid = "_timeline"
    existing_ids = {s["id"] for s in steps}
    n = 0
    while sid in existing_ids:
        n += 1
        sid = f"_timeline_{n}"
    steps.append(
        {
            "id": sid,
            "timeline": anchor_id,
            "relation": "the moment around it",
            "radius_minutes": 20,
            "max_per_side": 6,
            "gap_minutes": 15,
            "merge_gap_seconds": 300,
            "collapse_sources": _TIMELINE_COLLAPSE_SOURCES,
            "limit": 12,
        }
    )
    return sid


# Hard cap on the rendered timeline prose. Beyond this, trailing strips
# get truncated with an "… (N more strips)" suffix. Keeps `as_prompt`
# from blowing context for MCP / chat-LLM / web-chat consumers — the
# witness substrate's own 600-char per-item truncation is upstream of
# this; this cap is for everyone else who reads the full render.
_TIMELINE_RENDER_BUDGET_CHARS = 6000


# ── Rendering ───────────────────────────────────


# Valence rerank — mirrors delta-store's query.py:_valence_modifier so the
# compositional plan path applies the same affirms/refutes lift the shallow
# path does. Lives here (not in delta-store) because deep recall fetches
# clouds on the api side after the plan returns; pulling the modifier with
# it keeps the suppression contract symmetric without coupling the
# executor to engagement-cloud HTTP.
_VALENCE_MAX_PCT = 0.30


def _valence_score(cloud: list[dict]) -> float:
    if not cloud:
        return 0.0
    score = 0.0
    for d in cloud:
        tags = d.get("tags") or []
        for t in tags:
            if t.startswith("refutes:"):
                score -= 1.0
                break
            if t.startswith("affirms:"):
                score += 1.0
                break
            if t.startswith("from:"):
                score += 0.5
                break
            if t.startswith("engages:") or t.startswith("reply-to:"):
                score += 0.25
                break
        if "engagement:less" in tags:
            score -= 0.5
        elif "engagement:more" in tags:
            score += 0.5
    return score


def _valence_modifier(cloud: list[dict]) -> float:
    """≤1 boosts (lower distance = better), ≥1 demotes."""
    score = _valence_score(cloud)
    shift = max(-_VALENCE_MAX_PCT, min(_VALENCE_MAX_PCT, score * 0.05))
    return 1.0 - shift


def _apply_valence_rerank(deltas_by_step: dict[str, list[dict]]) -> None:
    """Multiply each delta's distance by its valence modifier and re-sort
    each step's list. Mutates in place. Deltas without a distance keep
    their input order — relevant for filter/aggregate steps that produced
    no semantic distance to begin with."""
    for deltas in deltas_by_step.values():
        any_distance = False
        for d in deltas:
            cloud = d.get("engagement_cloud") or []
            base = d.get("distance")
            if base is None or not cloud:
                continue
            d["distance"] = float(base) * _valence_modifier(cloud)
            any_distance = True
        if any_distance:
            deltas.sort(
                key=lambda d: (d.get("distance") is None, d.get("distance") or 0.0)
            )


_CLOUD_LABEL_BY_PREFIX = {
    "refutes": "refuted",
    "affirms": "affirmed",
    "from": "cited by sediment",
    "reply-to": "replied to",
}
_CLOUD_LABEL_BY_ENGAGEMENT = {
    "engagement:less": "disliked",
    "engagement:more": "liked",
    "engagement:chat": "chat reaction",
}
_CLOUD_RENDER_CAP = 5
_CLOUD_EXCERPT_CHARS = 80


def _cloud_label(member: dict) -> str | None:
    """Pick a single human label for an engagement-cloud member."""
    tags = member.get("tags") or []
    for t in tags:
        prefix = t.split(":", 1)[0]
        if prefix in _CLOUD_LABEL_BY_PREFIX:
            return _CLOUD_LABEL_BY_PREFIX[prefix]
        if t in _CLOUD_LABEL_BY_ENGAGEMENT:
            return _CLOUD_LABEL_BY_ENGAGEMENT[t]
    if any(t.startswith("engages:") for t in tags):
        return "engaged"
    return None


def _render_cloud(cloud: list[dict]) -> str:
    if not cloud:
        return ""
    lines: list[str] = []
    for member in cloud[:_CLOUD_RENDER_CAP]:
        label = _cloud_label(member)
        if not label:
            continue
        excerpt = (member.get("content") or "").strip().replace("\n", " ")
        excerpt = excerpt[:_CLOUD_EXCERPT_CHARS]
        if excerpt:
            lines.append(f"  — {label}: {excerpt}")
        else:
            lines.append(f"  — {label}")
    if not lines:
        return ""
    return "\n" + "\n".join(lines)


def _delta_line(d: dict) -> str:
    src = d.get("source", "unknown")
    ts = (d.get("timestamp") or "")[:16]
    tags = ", ".join((d.get("tags") or [])[:4])
    media = f"\n[Image attached: media_hash={d['media_hash']}]" if d.get("media_hash") else ""
    content = (d.get("content") or "")[:_MAX_CONTENT_CHARS]
    cloud_note = _render_cloud(d.get("engagement_cloud") or [])
    return f"[{src} · {ts} · {tags}]{media}\n{content}{cloud_note}"


def _format_strip_header(t_start: str, t_end: str) -> str:
    """Date · time-span header for a timeline strip."""
    if "T" in t_start and "T" in t_end:
        date_part = t_start.split("T", 1)[0]
        s_time = t_start.split("T", 1)[1][:5]
        e_time = t_end.split("T", 1)[1][:5]
        if s_time == e_time:
            return f"{date_part} · {s_time}"
        return f"{date_part} · {s_time}–{e_time}"
    return f"{t_start} – {t_end}"


def _render_timelines(timelines: list[dict], *, query: str) -> str:
    """Render a list of timeline strips as the LLM-facing markdown block.

    Strips are delimited by a thick rule with a date/time-range header,
    then within each strip:
      1. ANCHOR LINES first — the deltas the query actually matched.
         These are the load-bearing signal; consumers that truncate the
         render (witness substrate at 600 chars, etc.) see these first.
      2. Ambient context after — the surrounding deltas, rendered
         chronologically. Bursty telemetry sources are run-length
         collapsed by the executor before they hit this layer, so what
         arrives here is real moment texture, not heartbeat noise.

    Anchor lines carry a ``▸`` marker; ambient lines do not. Tag-keyed
    dispatch in ``timeline_renderers`` formats each line.

    Honors a hard char budget (``_TIMELINE_RENDER_BUDGET_CHARS``):
    strips render in order until the budget is exhausted, then a
    summary line reports how many more strips were skipped. Anchors
    of a partially-rendered strip always emit; ambient is the first
    thing dropped. Per-strip the dispatch caps individual lines, so
    the budget acts as a strip-count limiter rather than a per-line
    truncator.
    """
    from . import timeline_renderers

    if not timelines:
        return ""

    blocks: list[str] = []
    blocks.append(f'your query "{query}" returned')
    used = len(blocks[0])
    skipped_strips = 0
    rule = "═" * 8

    for i, tl in enumerate(timelines):
        header = _format_strip_header(tl.get("t_start", ""), tl.get("t_end", ""))
        anchor_count = len(tl.get("anchor_ids") or [])
        anchor_note = f" · {anchor_count} anchor" + ("s" if anchor_count != 1 else "")
        header_line = f"\n{rule} {header}{anchor_note} {rule}"

        deltas = tl.get("deltas") or []
        anchors = [d for d in deltas if _normalize_delta(d).get("is_anchor")]
        ambient = [d for d in deltas if not _normalize_delta(d).get("is_anchor")]

        anchor_lines = [
            timeline_renderers.render_delta(_normalize_delta(d)) for d in anchors
        ]
        anchor_lines = [ln for ln in anchor_lines if ln]
        anchor_block_size = len(header_line) + sum(len(ln) + 1 for ln in anchor_lines)

        # If even the anchor block alone won't fit, skip this strip and
        # all that follow — count them in skipped_strips for the tail.
        if used + anchor_block_size > _TIMELINE_RENDER_BUDGET_CHARS and i > 0:
            skipped_strips = len(timelines) - i
            break

        blocks.append(header_line)
        used += len(header_line)
        for ln in anchor_lines:
            blocks.append(ln)
            used += len(ln) + 1

        # Ambient is the first thing dropped under budget pressure.
        if anchors and ambient:
            ambient_lines = [
                timeline_renderers.render_delta(_normalize_delta(d)) for d in ambient
            ]
            ambient_lines = [ln for ln in ambient_lines if ln]
            divider = "  ── surrounding context ──"
            divider_emitted = False
            for ln in ambient_lines:
                projected = used + (0 if divider_emitted else len(divider) + 1) + len(ln) + 1
                if projected > _TIMELINE_RENDER_BUDGET_CHARS:
                    break
                if not divider_emitted:
                    blocks.append(divider)
                    used += len(divider) + 1
                    divider_emitted = True
                blocks.append(ln)
                used += len(ln) + 1

        # Inter-timeline associative tail (placeholder for explicit edges
        # later — when from:/chain provenance is wired in).
        if i < len(timelines) - 1:
            tail = "\n  …which led to…"
            if used + len(tail) <= _TIMELINE_RENDER_BUDGET_CHARS:
                blocks.append(tail)
                used += len(tail)

    if skipped_strips > 0:
        blocks.append(
            f"\n  … ({skipped_strips} more strip"
            + ("s" if skipped_strips != 1 else "")
            + " not shown — budget cap)"
        )

    return "\n".join(blocks)


def _normalize_delta(d) -> dict:
    """Accept either a dict or a pydantic-shaped object (TimelineDelta
    arrives over the wire as a dict already, but if anyone passes a
    pydantic model in tests, handle it)."""
    if isinstance(d, dict):
        return d
    if hasattr(d, "model_dump"):
        return d.model_dump()
    return dict(d)


def _render_tree(tree: list[dict], deltas_by_step: dict[str, list[dict]]) -> str:
    """Walk tree in order, emit 'relation — header:' blocks of deltas.

    Each delta surfaces only once, in the first step that contains it,
    so later union/chain steps don't rehash memories already shown.
    """
    blocks: list[str] = []
    seen: set[str] = set()

    for node in tree:
        deltas = deltas_by_step.get(node["id"], [])
        unique = []
        for d in deltas:
            did = d.get("id")
            if did and did in seen:
                continue
            unique.append(d)
            if did:
                seen.add(did)
        if not unique:
            continue

        relation = node.get("relation") or _DEFAULT_RELATION_BY_ACTION.get(
            node.get("action", ""), "surfaced"
        )
        header_parts = [relation]
        q = node.get("query")
        if isinstance(q, str) and q:
            header_parts.append(f'"{q}"')
        elif isinstance(q, list) and q:
            header_parts.append(f"from {' + '.join(str(x) for x in q)}")
        header = " — ".join(header_parts) + ":"

        body = "\n\n".join(_delta_line(d) for d in unique)
        blocks.append(f"{header}\n\n{body}")

    return "\n\n---\n\n".join(blocks)


# ── Main entry point ────────────────────────────


async def search(
    text: str,
    depth: str = "deep",
    session_slug: str | None = None,
    conv_context: str = "",
    limit: int = 50,
    threshold: float | None = None,
    view: str = "timeline",
) -> dict:
    """Canonical NL recall.

    ``depth="deep"``    — planner LLM composes a multi-step plan, DAG preserved.
    ``depth="shallow"`` — single semantic search, one-node tree.

    ``threshold`` (shallow only) drops results whose distance > threshold.

    ``view="timeline"`` (default) — append a timeline expansion to the
    plan and return chronological strips around each hit. ``as_prompt``
    is the rendered timeline; ``timelines`` carries the structured shape.
    Recall reads as moments (anchor + ambient context, gap-bounded), not
    orphan fragments stripped of conversation.

    ``view="deltas"`` — opt-out for callers that want the legacy flat
    per-step delta lists with the tree-of-blocks ``as_prompt``. Tests
    and table-rendering UIs use this.

    Retrieval counting lives at the delta-store (see delta-store's
    retrievals.py) so the Stats Activity card catches every client.
    """
    if not text or not text.strip():
        return _empty_result()

    if depth == "shallow":
        return await _shallow(text, limit=limit, threshold=threshold, view=view)
    return await _deep(
        text,
        conv_context=conv_context,
        session_slug=session_slug,
        limit=limit,
        view=view,
    )


async def _shallow(
    text: str, *, limit: int, threshold: float | None, view: str = "deltas"
) -> dict:
    # Shallow timeline-view runs the search via the plan executor instead
    # of the bare /search endpoint so the timeline step has a parent to
    # reference. One round-trip; same hit set as the legacy path because
    # the executor's _exec_search uses the same noise rerank.
    if view == "timeline":
        plan_steps = [
            {"id": "root", "search": text, "limit": limit, "relation": "surfaced"},
        ]
        plan = {"steps": plan_steps}
        _inject_timeline_step(plan)
        try:
            result = await delta_client.plan(plan["steps"])
        except Exception:
            return _empty_result(plan=plan)
        return await _build_result_from_plan_response(
            text=text,
            plan=plan,
            response=result,
            view="timeline",
            do_sediment=False,
        )

    try:
        data = await delta_client.search(text, limit=limit)
    except Exception:
        return _empty_result()
    raw = data.get("results", []) or []
    if threshold is not None:
        raw = [r for r in raw if r.get("distance", 1.0) <= threshold]

    deltas: list[dict] = []
    media_hashes: list[str] = []
    for r in raw:
        d = dict(r.get("delta") or r)
        if "id" not in d and "delta_id" in d:
            d["id"] = d["delta_id"]
        deltas.append(d)
        if d.get("media_hash"):
            media_hashes.append(d["media_hash"])

    node = {
        "id": "root",
        "relation": "what came to mind",
        "parents": [],
        "action": "search",
        "query": text,
        "delta_ids": [d.get("id") for d in deltas if d.get("id")],
    }
    tree = [node] if deltas else []
    deltas_by_step = {"root": deltas} if deltas else {}

    return {
        "plan": {"steps": [{"id": "root", "search": text, "limit": limit}]},
        "tree": tree,
        "deltas_by_step": deltas_by_step,
        "total_count": len(deltas),
        "media_hashes": media_hashes[:_MAX_MEDIA_HASHES],
        "as_prompt": _render_tree(tree, deltas_by_step),
        "thinking_prose": None,
        "thinking_id": None,
        "timelines": [],
    }


async def _deep(
    text: str,
    *,
    conv_context: str,
    session_slug: str | None,
    limit: int,
    view: str = "deltas",
) -> dict:
    plan = await _generate_plan(text, conv_context=conv_context, session_slug=session_slug)
    if not plan:
        return _empty_result()

    if view == "timeline":
        _inject_timeline_step(plan)

    try:
        result = await delta_client.plan(plan["steps"])
    except Exception:
        return _empty_result(plan=plan)

    return await _build_result_from_plan_response(
        text=text,
        plan=plan,
        response=result,
        view=view,
        do_sediment=True,
    )


async def _build_result_from_plan_response(
    *,
    text: str,
    plan: dict,
    response: dict,
    view: str,
    do_sediment: bool,
) -> dict:
    """Shared post-plan processing — used by both deep and shallow-timeline.

    Walks the plan, separates delta-result steps from timeline-result
    steps, runs sediment-provenance + engagement-cloud + valence rerank
    on the delta side, and pulls the timeline payload out as a top-level
    field when present.
    """
    steps_data = response.get("steps", {}) or {}
    tree: list[dict] = []
    deltas_by_step: dict[str, list[dict]] = {}
    media_hashes: list[str] = []
    seen_ids: set[str] = set()
    timelines: list[dict] = []

    for step in plan["steps"]:
        sid = step["id"]
        action, val = _action_of(step)
        step_payload = steps_data.get(sid, {}) or {}

        if action == "timeline":
            tls = step_payload.get("timelines") or []
            timelines.extend(tls)
            tree.append(
                {
                    "id": sid,
                    "relation": step.get("relation")
                    or _DEFAULT_RELATION_BY_ACTION.get(action, "the moment around it"),
                    "parents": _parents_of(step),
                    "action": action,
                    "query": val if isinstance(val, str) else None,
                    "delta_ids": [],
                }
            )
            continue

        raw_deltas = step_payload.get("deltas", []) or []
        cleaned: list[dict] = []
        for d in raw_deltas:
            tags = d.get("tags") or []
            if "assistant" in tags and ("fathom-chat" in tags or d.get("source") == "fathom-chat"):
                continue
            did = d.get("id")
            if did:
                if did in seen_ids:
                    continue
                seen_ids.add(did)
            cleaned.append(d)
            if d.get("media_hash"):
                media_hashes.append(d["media_hash"])

        relation = step.get("relation") or _DEFAULT_RELATION_BY_ACTION.get(action, "surfaced")
        query = val if action == "search" else val if isinstance(val, (str, list)) else None

        tree.append(
            {
                "id": sid,
                "relation": relation,
                "parents": _parents_of(step),
                "action": action,
                "query": query,
                "delta_ids": [d.get("id") for d in cleaned if d.get("id")],
            }
        )
        deltas_by_step[sid] = cleaned

    await _expand_sediment_provenance(plan, tree, deltas_by_step, seen_ids)
    await _attach_engagement_clouds(deltas_by_step, seen_ids)
    _apply_valence_rerank(deltas_by_step)

    if do_sediment:
        thinking_prose, thinking_id = await _synthesize_thinking(text, deltas_by_step)
    else:
        thinking_prose, thinking_id = None, None

    if view == "timeline" and timelines:
        as_prompt = _render_timelines(timelines, query=text)
    else:
        as_prompt = _render_tree(tree, deltas_by_step)

    return {
        "plan": plan,
        "tree": tree,
        "deltas_by_step": deltas_by_step,
        "total_count": len(seen_ids),
        "media_hashes": media_hashes[:_MAX_MEDIA_HASHES],
        "as_prompt": as_prompt,
        "thinking_prose": thinking_prose,
        "thinking_id": thinking_id,
        "timelines": timelines,
    }


# How many provenance ids to chase per recall — caps the worst case where
# a sediment with dozens of `from:` pointers and another sediment beside it
# blow the trail up. The expansion is associative chrome, not load-bearing
# retrieval; if the ceiling truncates, the original sediment still surfaces
# with its full content.
_SEDIMENT_PROVENANCE_LIMIT = 24


def _provenance_ids_from_deltas(
    deltas: list[dict], already_seen: set[str]
) -> list[str]:
    """Pull `from:<id>` pointers off any kind:sediment delta, drop already-
    seen ids, preserve first-seen order, dedupe."""
    out: list[str] = []
    seen: set[str] = set()
    for d in deltas:
        tags = d.get("tags") or []
        if "kind:sediment" not in tags:
            continue
        for t in tags:
            if not t.startswith("from:"):
                continue
            ref = t[len("from:") :].strip()
            if not ref or ref in already_seen or ref in seen:
                continue
            seen.add(ref)
            out.append(ref)
    return out


async def _expand_sediment_provenance(
    plan: dict,
    tree: list[dict],
    deltas_by_step: dict[str, list[dict]],
    seen_ids: set[str],
) -> None:
    """If any surfaced delta is a sediment, fetch the deltas it cites
    via `from:<id>` and append them as a synthetic `_provenance` step.

    The expansion shows up in the rendered trail as its own block so the
    reader can see "and what that came from", and the cited deltas
    participate in valence rerank and sediment synthesis the same way as
    everything else. Fail-soft — a fetch error leaves the original
    sediment trail intact.
    """
    all_deltas: list[dict] = []
    for step_deltas in deltas_by_step.values():
        all_deltas.extend(step_deltas)
    refs = _provenance_ids_from_deltas(all_deltas, seen_ids)
    if not refs:
        return
    refs = refs[:_SEDIMENT_PROVENANCE_LIMIT]
    try:
        fetched = await delta_client.batch_get(refs)
    except Exception:
        log.exception("search: sediment provenance batch-get failed")
        return
    if not fetched:
        return

    # Drop anything that was already in the result set (dedup defensive —
    # _provenance_ids_from_deltas already filtered seen_ids, but the lake
    # could have surfaced the source independently between steps).
    fresh: list[dict] = []
    for d in fetched:
        did = d.get("id")
        if not did or did in seen_ids:
            continue
        seen_ids.add(did)
        fresh.append(d)
    if not fresh:
        return

    sid = "_provenance"
    deltas_by_step[sid] = fresh
    tree.append(
        {
            "id": sid,
            "relation": "and what that came from",
            "parents": [],
            "action": "search",
            "query": None,
            "delta_ids": [d.get("id") for d in fresh if d.get("id")],
        }
    )
    plan_steps = plan.get("steps") if isinstance(plan, dict) else None
    if isinstance(plan_steps, list):
        plan_steps.append(
            {
                "id": sid,
                "search": "<sediment provenance expansion>",
                "limit": len(fresh),
                "relation": "and what that came from",
            }
        )


async def _attach_engagement_clouds(
    deltas_by_step: dict[str, list[dict]],
    delta_ids: set[str],
) -> None:
    """Batched cloud lookup for every surfaced delta; mutates the dicts in place.

    Fails soft — a cloud fetch error still lets recall return its trail.
    """
    if not delta_ids:
        return
    try:
        cloud_by_id = await delta_client.engagement_cloud(sorted(delta_ids))
    except Exception:
        log.exception("search: engagement cloud fetch failed")
        return
    if not cloud_by_id:
        return
    for deltas in deltas_by_step.values():
        for d in deltas:
            did = d.get("id")
            if did and cloud_by_id.get(did):
                d["engagement_cloud"] = cloud_by_id[did]


def _empty_result(plan: dict | None = None) -> dict:
    return {
        "plan": plan or {"steps": []},
        "tree": [],
        "deltas_by_step": {},
        "total_count": 0,
        "media_hashes": [],
        "as_prompt": "",
        "thinking_prose": None,
        "thinking_id": None,
        "timelines": [],
    }
