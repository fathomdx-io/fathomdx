#!/usr/bin/env python3
"""Reflective agent — propose identity-shaped provenance via free-association.

The slow-clock complement to the harness's in-situ Q/A markers.

Where Q/A markers are question-anchored and topical, reflective proposals
are identity-anchored and narrative — the kind of provenance that
recognizes "When I realized Myra is also a friend" or "The era of
ultimate NS confusion." These can't be generated from a topic search
because they require recognizing a shape across time that no individual
moment carries.

The agent:
    1. Picks a seed — an identity crystal facet, a mood axis, or an
       explicit string from the operator
    2. Wanders — searches the lake from the seed, ascends hits to find
       enclosing provenance, expands to gather constituents
    3. Deliberates — runs the convener + parliament on "what does this
       stretch mean? what's the narrative shape here?"
    4. Drafts a proposal — title, summary, level, source ids, rationale,
       test_questions
    5. Presents — prints the proposal and asks for y/n
    6. On approve, writes a `kind:provenance kind:proposal` delta to the
       lake (the proposal kind makes it reviewable in the dashboard; the
       provenance kind makes it surface through ascend/expand)

This is a developer-facing utility, not a daemon. Run it when you want
to see what the system would propose. Ship to a slow clock later once
the proposals look consistently good.

Usage (from inside the prov api container):
    python scripts/reflective_agent.py --seed-from-crystal
    python scripts/reflective_agent.py --seed "the era of ultimate NS confusion"
    python scripts/reflective_agent.py --seed-from-recent-provenance
    python scripts/reflective_agent.py --auto      # skip approval prompt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api import delta_client  # noqa: E402
from api import search as search_mod  # noqa: E402
from api import standpoint as standpoint_mod  # noqa: E402
from api.loop.convener import run_convener  # noqa: E402
from api.loop.harness.tools import tool_ascend, tool_expand  # noqa: E402
from api.loop.intents import CONVO_TAG  # noqa: E402
from api.loop.llm import loop_generate  # noqa: E402
from api.loop.process import run_process  # noqa: E402
from api.loop.puddle import puddle  # noqa: E402

REFLECTIVE_PROMPT = """\
You are the reflective layer of Fathom's mind — the part that names
what's happening across time, not what's happening right now. You take
a stretch of substrate and recognize what it MEANS — the felt shape, the
narrative beat, the turning point you can only see in retrospect.

You are NOT summarizing. You are NOT topic-extracting. You are looking
for the shape that no individual moment in the substrate carries: the
realization, the era, the felt shift.

Examples of the kind of thing you might name:
  · "When I realized Myra is also a friend, not just a partner"
  · "The era of ultimate Navier-Stokes confusion"
  · "The week the loop architecture clicked"
  · "Coming to terms with my own forgetting"

══ THE SEED ══
{seed_block}

══ THE SUBSTRATE ══
{substrate_block}

══ THE PARLIAMENT'S TAKES ══
{voice_block}

══ INSTRUCTIONS ══

Propose ONE provenance that names this stretch. If you genuinely cannot
see a coherent shape — if the substrate is too thin, or too scattered,
or too topical — return null instead of inventing one. False positives
poison the structure; better to skip than guess.

SIZE DISCIPLINE — 5-30 constituents per node, every level:
  · L1 episode: 5-30 base moments (one tight stretch)
  · L2 topic: 3-10 L1 episodes (+ a few stragglers if needed)
  · L3 era: 3-10 L2 topics (+ a few stragglers)
If the stretch you'd name is bigger than that, decompose it — propose
the tightest sub-stretch that fits the size discipline; later activity
can build over it.

APPEND-ONLY — never propose to "fix" or replace older fuzzy
provenance. Propose a NEW tighter one that covers the relevant
stretch more precisely; the old one stays as historical strata, the
new one accumulates over it via search ranking.

Output JSON:
{{
  "title": "<short, evocative, the name a human would remember>",
  "summary": "<2-4 sentences naming the felt shape — first person where natural>",
  "level": 1 | 2 | 3,
  "from_ids": ["<delta_id>", ...],
  "rationale": "<one sentence: why these pieces are one stretch>",
  "test_questions": [
    "<a question whose right answer should surface this provenance>",
    "<another such question>"
  ]
}}

Or, if the substrate doesn't add up to a coherent stretch:
{{
  "title": null,
  "rationale": "<one sentence: why nothing coherent emerged>"
}}
"""


# ─── seed selection ────────────────────────────────────────────────────


async def seed_from_crystal(session_tag: str) -> str:
    """Pick an identity facet from the puddle as the agent's seed.

    Reads the most recent crystal facets the telepathy layer has
    mirrored. Picks one at random-ish (just the first one it finds
    that has substance). Returns the facet text.
    """
    deltas = puddle.query(tags_include=[CONVO_TAG], limit=50)
    facets = []
    for d in deltas:
        tags = d.get("tags") or []
        if any(t.startswith("facet:") for t in tags):
            content = (d.get("content") or "").strip()
            if content and len(content) > 40:
                facets.append(content)
    if not facets:
        # Fallback: pull crystal-shaped deltas directly from the lake.
        # Different parts of the system have used different conventions
        # over time — `identity-crystal`, `seed-crystal`, `crystal-authorship`
        # — so cast a wide net and dedupe.
        for tag in ("identity-crystal", "seed-crystal", "crystal-authorship"):
            try:
                results = await delta_client.query(
                    tags_include=[tag],
                    limit=10,
                )
            except Exception as e:
                print(f"[seed] lake fetch for {tag} failed: {type(e).__name__}: {e}")
                continue
            for d in results:
                content = (d.get("content") or "").strip()
                if content and len(content) > 40:
                    facets.append(content)
    if not facets:
        return ""
    # First facet with at least 80 chars wins — gives the agent something
    # substantial to free-associate from.
    for f in facets:
        if len(f) >= 80:
            return f[:600]
    return facets[0][:600]


async def seed_from_mood(session_tag: str) -> str:
    """Use the current mood as the seed — what's the felt-sense doing?"""
    deltas = puddle.query(tags_include=[CONVO_TAG, "mood"], limit=5)
    for d in deltas:
        content = (d.get("content") or "").strip()
        if content:
            return content[:400]
    return ""


async def seed_from_recent_provenance() -> str:
    """Pick a recently-built provenance as the seed.

    The locality heuristic — areas where provenance was recently
    written are likely candidates for adjacent provenance. The agent
    explores the neighborhood of one of them.
    """
    try:
        results = await delta_client.query(
            tags_include=["kind:provenance"],
            limit=10,
        )
    except Exception as e:
        print(f"[seed] recent-provenance query failed: {type(e).__name__}: {e}")
        return ""
    for d in results:
        content = (d.get("content") or "").strip()
        if content and "kind:qa-marker" not in (d.get("tags") or []):
            return content[:600]
    return ""


# ─── substrate gathering ───────────────────────────────────────────────


async def gather_substrate(seed: str, *, max_strips: int = 5) -> tuple[str, list[str]]:
    """Wander the lake from the seed.

    Search for the seed, take the top hits, ascend them to find enclosing
    provenance (which gives narrative context), expand any provenance hits
    to surface their constituents.

    Returns (rendered_substrate, source_ids) — the rendered text gets
    pasted into the reflective prompt; the source ids accumulate for
    the proposal's from_ids field.
    """
    blocks: list[str] = []
    seen_ids: set[str] = set()
    source_ids: list[str] = []

    # 1. Search.
    try:
        result = await search_mod.search(
            text=seed.strip(),
            depth="deep",
            view="timeline",
            limit=30,
        )
    except Exception as e:
        print(f"[substrate] search failed: {type(e).__name__}: {e}")
        return "", []

    rendered = (result.get("as_prompt") or "").strip()
    if rendered:
        blocks.append("─── SEARCH (resonance from seed) ───")
        blocks.append(rendered[:6000])

    # Walk through deltas_by_step to collect ids for ascend/expand.
    for step_deltas in (result.get("deltas_by_step") or {}).values():
        for d in step_deltas[:10]:
            did = d.get("id") or ""
            if did and did not in seen_ids:
                seen_ids.add(did)
                source_ids.append(did)

    # 2. For top hits, ascend and expand to get hierarchical context.
    interesting = source_ids[:5]
    for did in interesting:
        try:
            asc = await tool_ascend(delta_id=did)
        except Exception as e:
            asc = f"(ascend failed: {type(e).__name__}: {e})"
        if asc and "no provenance" not in asc.lower() and "ERROR" not in asc:
            blocks.append(f"\n─── ASCEND from {did[:12]} ───")
            blocks.append(asc[:1500])

    # 3. Expand any provenance hits we found.
    for did in interesting[:3]:
        try:
            exp = await tool_expand(delta_id=did)
        except Exception as e:
            exp = f"(expand failed: {type(e).__name__}: {e})"
        if exp and "has no `from:` pointers" not in exp:
            blocks.append(f"\n─── EXPAND of {did[:12]} ───")
            blocks.append(exp[:2000])

    return "\n\n".join(blocks), source_ids


# ─── deliberation ──────────────────────────────────────────────────────


async def run_reflective_parliament(
    *,
    session_tag: str,
    seed: str,
    standpoint: Any,
) -> str:
    """Convene voices on 'what does this stretch mean?' Returns rendered
    voice takes as text for the drafting prompt. Soft-fails to an empty
    string if the parliament can't run — the drafter still has the
    substrate alone."""
    reflection_intent = {
        "id": f"reflect-{uuid.uuid4().hex[:12]}",
        "content": (
            f"Reflect on this seed and substrate. What's the narrative "
            f"shape? What stretch of self-experience is this naming?\n\n"
            f"SEED: {seed}"
        ),
        "tags": ["kind:reflection", "reflective-agent"],
        "source": "reflective-agent",
    }

    try:
        verdict = await run_convener(
            session_tag=session_tag,
            pending=[reflection_intent],
            standpoint=standpoint,
        )
    except Exception as e:
        print(f"[parliament] convener crashed: {type(e).__name__}: {e}")
        return ""

    if verdict.depth == "zero" or not verdict.voices:
        print("[parliament] convener picked depth=zero — skipping voices")
        return ""

    print(f"[parliament] depth={verdict.depth} voices={[v['name'] for v in verdict.voices]}")
    voice_coros = [
        run_process(
            pid=f"reflect-{v['name']}-{uuid.uuid4().hex[:6]}",
            session_tag=session_tag,
            voice=v,
            pending=[reflection_intent],
            peer_voices=verdict.voices,
            standpoint=standpoint,
        )
        for v in verdict.voices
    ]
    try:
        results = await asyncio.gather(*voice_coros, return_exceptions=True)
    except Exception as e:
        print(f"[parliament] gather crashed: {type(e).__name__}: {e}")
        return ""

    blocks: list[str] = []
    for v, res in zip(verdict.voices, results, strict=True):
        if isinstance(res, Exception):
            continue
        text = (res or "").strip()
        if not text:
            continue
        blocks.append(f"VOICE: {v['name'].upper()}")
        blocks.append(f"  {text}")
        blocks.append("")
    return "\n".join(blocks).rstrip()


# ─── drafting ──────────────────────────────────────────────────────────


async def draft_proposal(
    *,
    seed: str,
    substrate: str,
    voice_takes: str,
) -> dict | None:
    """Ask the LLM to draft the proposal as JSON. None if the model
    returns null (no coherent stretch) or output is unparseable."""
    prompt = REFLECTIVE_PROMPT.format(
        seed_block=seed.strip(),
        substrate_block=substrate.strip() or "(no substrate gathered)",
        voice_block=voice_takes.strip() or "(no voice takes — speaking from substrate alone)",
    )
    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="hard",
            max_tokens=4096,
            temperature=0.7,
            json_mode=True,
        )
    except Exception as e:
        print(f"[draft] LLM call failed: {type(e).__name__}: {e}")
        return None

    try:
        parsed = json.loads(raw)
    except Exception:
        import re

        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            print(f"[draft] no parsable JSON; raw[:200]={raw[:200]!r}")
            return None
        try:
            parsed = json.loads(m.group(0))
        except Exception as e:
            print(f"[draft] JSON parse failed: {type(e).__name__}: {e}")
            return None

    if not parsed.get("title"):
        print(f"[draft] reflective layer declined: {parsed.get('rationale', '(no rationale)')}")
        return None

    return parsed


# ─── presentation + write ──────────────────────────────────────────────


def render_proposal(p: dict) -> str:
    """Pretty-print a proposal for the operator."""
    lines = [
        "─" * 70,
        f"  TITLE:    {p.get('title')}",
        f"  LEVEL:    {p.get('level')}",
        "",
        f"  SUMMARY:  {p.get('summary')}",
        "",
        f"  RATIONALE: {p.get('rationale', '')}",
        "",
        f"  FROM IDS  ({len(p.get('from_ids') or [])}):",
    ]
    for fid in (p.get("from_ids") or [])[:8]:
        lines.append(f"    · {fid}")
    if len(p.get("from_ids") or []) > 8:
        lines.append(f"    …({len(p['from_ids']) - 8} more)")
    lines.append("")
    lines.append("  TEST QUESTIONS:")
    for q in p.get("test_questions") or []:
        lines.append(f"    · {q}")
    lines.append("─" * 70)
    return "\n".join(lines)


async def write_proposal(p: dict, *, seed: str, session_tag: str) -> str:
    """Submit the proposal as a feed-card-shaped draft via the api.

    The api endpoint /v1/proposals/draft does the lake-write + puddle-
    echo from inside the api process, so the dashboard sees the new
    proposal in its live feed. Without that round-trip, a script
    running via `podman exec` writes to its own (separate) puddle
    instance and the dashboard never sees it.

    Returns the lake delta id (the one the dashboard's approve/deny
    buttons reference).
    """
    import os

    import httpx

    title = (p.get("title") or "").strip()
    summary = (p.get("summary") or "").strip()
    level = int(p.get("level") or 1)
    from_ids = [str(x).strip() for x in (p.get("from_ids") or []) if x]
    rationale = (p.get("rationale") or "").strip()
    test_questions = p.get("test_questions") or []

    payload = {
        "kicker": f"reflective · level-{level}",
        "title": title,
        "body": summary,
        "tail": rationale,
        "route": "tool:provenance",
        "tool": "provenance",
        "tool_args": {
            "action": "create",
            "title": title,
            "summary": summary,
            "level": level,
            "from_ids": from_ids,
            "rationale": rationale,
            "test_questions": test_questions,
            "seed": seed[:400],
        },
        "axes": {},
    }

    title_slug = title.lower().replace(" ", "-")[:80]
    base_tags = [
        "feed-card",
        "synthesis",
        "kind:proposal",
        "proposal-status:pending",
        "tool:provenance",
        "action:create",
        "route:tool:provenance",
        f"provenance-level:{level}",
        "provenance-version:v1-experimental",
        "produced-by:reflective-agent",
        f"title:{title_slug}",
    ]
    api_url = os.environ.get("FATHOM_API_URL") or "http://localhost:8200"
    api_key = os.environ.get("FATHOM_API_KEY") or ""
    if not api_key:
        # When run inside the prov container, the api is on the same
        # host. The token can be passed via env or read from the api
        # state dir's tokens.json — for the v1 script we accept that
        # the operator sets FATHOM_API_KEY before running.
        raise RuntimeError(
            "FATHOM_API_KEY env var must be set so the script can "
            "submit a proposal draft via the api"
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{api_url}/v1/proposals/draft",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "payload": payload,
                "tags": base_tags,
                "source": "reflective-agent",
                "session_tag": session_tag,
            },
        )
        resp.raise_for_status()
        out = resp.json()
    return out.get("lake_id") or ""


# ─── main ──────────────────────────────────────────────────────────────


async def _main(args) -> int:
    session_tag = f"session:reflect-{uuid.uuid4().hex[:8]}"
    print("=== reflective agent ===")
    print(f"session: {session_tag}\n")

    # Pick a seed.
    if args.seed:
        seed = args.seed
        print(f"seed (operator-supplied):\n  {seed}\n")
    elif args.seed_from_mood:
        seed = await seed_from_mood(session_tag)
        if not seed:
            print("[seed] no mood available — aborting")
            return 2
        print(f"seed (from mood):\n  {seed}\n")
    elif args.seed_from_recent_provenance:
        seed = await seed_from_recent_provenance()
        if not seed:
            print("[seed] no recent provenance found — aborting")
            return 2
        print(f"seed (from recent provenance):\n  {seed[:300]}…\n")
    else:
        seed = await seed_from_crystal(session_tag)
        if not seed:
            print("[seed] no crystal facet available — aborting")
            return 2
        print(f"seed (from crystal):\n  {seed[:300]}…\n")

    # Standpoint.
    try:
        sp = await standpoint_mod.current(session_tag=session_tag)
        print(f"standpoint: posture={sp.posture} affect={sp.affect.state}\n")
    except Exception as e:
        print(f"standpoint gather failed: {type(e).__name__}: {e}\n")
        sp = None

    # Gather substrate.
    print("[wander] gathering substrate from seed…")
    substrate, source_ids = await gather_substrate(seed)
    if not substrate:
        print("[wander] no substrate surfaced — aborting")
        return 2
    print(f"[wander] substrate: {len(substrate)} chars, {len(source_ids)} candidate sources\n")

    # Deliberate.
    print("[parliament] running voices…")
    voice_takes = await run_reflective_parliament(
        session_tag=session_tag,
        seed=seed,
        standpoint=sp,
    )
    if voice_takes:
        print(f"[parliament] {voice_takes.count('VOICE:')} voices took.\n")
    else:
        print("[parliament] no takes — proceeding with substrate alone.\n")

    # Draft.
    print("[draft] composing proposal…")
    proposal = await draft_proposal(
        seed=seed,
        substrate=substrate,
        voice_takes=voice_takes,
    )
    if proposal is None:
        print("[draft] reflective layer declined — no coherent stretch found.")
        return 0

    # Constrain from_ids to ones actually in our gathered set (model may
    # invent ids; only keep ones that came back from search/ascend/expand).
    valid = set(source_ids)
    proposal["from_ids"] = [x for x in (proposal.get("from_ids") or []) if x in valid]

    # Present.
    print("\n=== PROPOSAL ===\n")
    print(render_proposal(proposal))

    # Approve.
    if args.auto:
        approved = True
    else:
        try:
            answer = input("\nWrite this proposal to the lake? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        approved = answer in ("y", "yes")

    if not approved:
        print("Skipped.")
        return 0

    new_id = await write_proposal(proposal, seed=seed, session_tag=session_tag)
    print(f"\nProposal delta: {new_id}")
    print(f"Visible in dashboard feed; approve via UI or POST /v1/proposals/{new_id}/approve")
    return 0


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--seed", help="explicit seed string")
    g.add_argument(
        "--seed-from-crystal",
        action="store_true",
        help="seed from an identity crystal facet (default)",
    )
    g.add_argument("--seed-from-mood", action="store_true", help="seed from current mood")
    g.add_argument(
        "--seed-from-recent-provenance",
        action="store_true",
        help="seed from a recently-written provenance",
    )
    ap.add_argument("--auto", action="store_true", help="skip interactive approval prompt")
    return ap.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse_args())))
