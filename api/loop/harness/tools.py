"""Harness tools — what Fathom can call from inside the agentic loop.

Four tools in Phase 1: `search`, `expand`, `ascend`, `deliberate`. Each
returns a string the model reads on its next turn (rendered transcript
goes back into the prompt). Errors are returned as strings rather than
raised so the loop can keep running and the model can adapt.

`deliberate` wraps the existing convener + parliament one-round path; it
does NOT reimplement deliberation. The harness inverts the relationship —
deliberation is now elective, called from within the harness when the
question calls for antagonism, instead of being a mandatory pre-step.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .. import resonance  # noqa: F401  — process imports it transitively
from ... import delta_client
from ... import search as search_mod
from ..convener import run_convener
from ..process import run_process
from ..puddle import puddle


# Hard caps on tool-result rendering — keep one tool's output from
# eating the entire prompt budget. The model can issue another search
# with a refined query if it needs more.
_SEARCH_RENDER_BUDGET = 4000
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
    if len(rendered) > _SEARCH_RENDER_BUDGET:
        rendered = rendered[:_SEARCH_RENDER_BUDGET] + "\n…(truncated; refine your query for more)"
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


# ─── tool dispatch ─────────────────────────────────────────────────────


# Tool registry — the loop driver looks up handlers here. Each handler
# is async, takes kwargs, returns a string.
TOOL_HANDLERS = {
    "search": tool_search,
    "expand": tool_expand,
    "ascend": tool_ascend,
    "deliberate": tool_deliberate,
}


# Args each tool accepts FROM THE MODEL — the harness adds session_tag /
# pending / standpoint to deliberate from its own context, so the model
# only supplies `question`. This map keeps the model from injecting
# unexpected kwargs into the call.
TOOL_MODEL_ARGS = {
    "search":     {"query", "depth"},
    "expand":     {"delta_id"},
    "ascend":     {"delta_id"},
    "deliberate": {"question"},
}
