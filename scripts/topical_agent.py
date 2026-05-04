#!/usr/bin/env python3
"""Topical agent — automated level-1 / level-2 provenance proposals.

The mechanical complement to the reflective agent. Where the reflective
agent free-associates from identity facets to surface narrative-shaped
provenance, the topical agent runs over time windows or accumulated
level-1 provenance and proposes thematic clusters.

Two modes:

  --level 1  : Pull deltas in a time window from the prov delta-store,
               compress them by source (counts for noise sources, sample
               content for dense ones), ask the LLM to cluster into
               self-contained episodes, post each cluster as a proposal.

  --level 2  : Pull all existing level-1 provenance, ask the LLM to
               propose topic groupings, post each topic as a proposal.

All proposals route through /v1/proposals/draft so the dashboard's
Edit/Deny/Approve flow handles them. On approve the existing
proposals.py handler writes the real kind:provenance delta.

Usage (from inside the prov api container):
    # Level-1 over a 6h window:
    python scripts/topical_agent.py --level 1 \\
        --window-start 2026-04-06T00:00:00Z \\
        --window-hours 6

    # Level-2 over existing level-1 provenance:
    python scripts/topical_agent.py --level 2

    # --auto skips per-cluster confirmation, drops every proposal
    # into the dashboard feed for batch review there.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api import delta_client  # noqa: E402
from api.loop.llm import loop_generate  # noqa: E402


# Sources whose deltas usually carry low signal — heartbeats, rss noise,
# sysinfo. We aggregate them into counts rather than passing content,
# which keeps the prompt budget bounded and stops the LLM from clustering
# around noise.
NOISE_SOURCES = frozenset({
    "laptop-health",
    "system-info",
    "rss",
    "rss/atlas-obscura",
    "rss/reddit-memes",
    "rss/self-hosted",
    "feed-pressure",
    "telepathy",
    "judge",
    "convener",
    "agent-heartbeat",
    "loop-metric",
})

# Per-source content sampling — for dense sources (claude-code, vault),
# we pass the actual content so the LLM has something to cluster on.
# Cap on samples per source so a single chatty source doesn't drown out
# the others.
MAX_SAMPLES_PER_SOURCE = 20
MAX_SAMPLE_CHARS = 240


# ─── shared helpers ────────────────────────────────────────────────────


def _slugify(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return out[:80] or "unnamed"


def _api_url() -> str:
    return os.environ.get("FATHOM_API_URL") or "http://localhost:8200"


def _api_key() -> str:
    key = os.environ.get("FATHOM_API_KEY") or ""
    if not key:
        raise RuntimeError(
            "FATHOM_API_KEY env var must be set so the script can "
            "submit proposal drafts via the api"
        )
    return key


async def _post_proposal_draft(
    *,
    payload: dict,
    tags: list[str],
    source: str,
    session_tag: str,
) -> str:
    """POST to /v1/proposals/draft. Returns the lake delta id."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_api_url()}/v1/proposals/draft",
            headers={"Authorization": f"Bearer {_api_key()}"},
            json={
                "payload": payload,
                "tags": tags,
                "source": source,
                "session_tag": session_tag,
            },
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("lake_id") or ""


def _build_proposal_payload(
    *,
    title: str,
    summary: str,
    level: int,
    from_ids: list[str],
    rationale: str,
    test_questions: list[str],
    seed_label: str,
) -> tuple[dict, list[str]]:
    """Construct the witness-card-shaped payload + base tag list for a
    provenance-proposal write."""
    payload = {
        "kicker": f"topical · level-{level}",
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
            "seed": seed_label[:400],
        },
        "axes": {},
    }
    title_slug = _slugify(title)
    tags = [
        "feed-card",
        "synthesis",
        "kind:proposal",
        "proposal-status:pending",
        "tool:provenance",
        "action:create",
        "route:tool:provenance",
        f"provenance-level:{level}",
        "provenance-version:v1-experimental",
        "produced-by:topical-agent",
        f"title:{title_slug}",
    ]
    return payload, tags


# ─── level-1 producer ──────────────────────────────────────────────────


L1_PROMPT = """\
You are the topical clustering layer of Fathom's mind. You take a time
window of raw activity and propose level-1 provenance — episodes,
self-contained units of meaning that group base moments by topic /
session / project / theme.

You are NOT writing identity-shaped retrospectives. That's the
reflective layer's job. You are writing TOPICAL summaries — "what
was being worked on", "what conversation happened", "what was being
read". Episodes that future-me can find by searching the topic.

══ THE WINDOW ══
{window_block}

══ ACTIVITY (compressed by source) ══
{activity_block}

══ INSTRUCTIONS ══

Propose 1-N level-1 provenances that cover the meaningful activity in
this window. Aim for 3-12 clusters typical of a 4-6h window. Skip
clusters where the substrate is too thin or too noisy to mean anything.

SIZE DISCIPLINE — 5-30 base moments per L1 episode. Tight clusters,
not bulk containers. If you'd be naming an episode with 50+ deltas,
that's two or more episodes that need decomposing — propose them
separately. Better to have 8 tight episodes than one 80-delta sprawl.

APPEND-ONLY — never propose to "fix" or replace existing provenance.
If the same area has older fuzzy provenance, propose a NEW tighter
one over a different (or overlapping) constituent set; the old one
stays as historical strata.

For each cluster, return:
  · title: short, evocative, the name a human would search for
  · summary: 2-4 sentences, in third or first person — what happened,
    what was being worked on, what the takeaway was
  · from_ids: 5-30 base-moment ids that belong to this cluster
  · rationale: one sentence — why these moments are one episode
  · test_questions: 1-3 questions whose right answer should surface
    this cluster

Return JSON:
{{
  "clusters": [
    {{
      "title": "...",
      "summary": "...",
      "from_ids": ["<id>", ...],
      "rationale": "...",
      "test_questions": ["...", ...]
    }},
    ...
  ]
}}

Or return {{"clusters": []}} if the window doesn't contain meaningful
clusterable activity (e.g. all noise sources).
"""


async def _pull_window(
    *, start_iso: str, end_iso: str, limit: int = 800
) -> list[dict]:
    """Fetch every delta in the window via delta-store HTTP."""
    delta_store_url = os.environ.get("FATHOM_DELTA_STORE_URL") or "http://delta-store:8000"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            f"{delta_store_url}/deltas",
            params={
                "time_start": start_iso,
                "time_end": end_iso,
                "limit": limit,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("results") or data.get("deltas") or []


def _compress_window_by_source(deltas: list[dict]) -> str:
    """Render the window as a per-source summary the LLM can cluster on.

    Dense sources surface samples. Noise sources just show a count.
    Keeps the prompt bounded regardless of how many deltas are in
    the window.
    """
    by_source: dict[str, list[dict]] = defaultdict(list)
    for d in deltas:
        src = (d.get("source") or "?").strip() or "?"
        by_source[src].append(d)

    blocks: list[str] = []
    # Sort: noise sources at the end, others by count desc.
    sorted_sources = sorted(
        by_source.items(),
        key=lambda kv: (kv[0] in NOISE_SOURCES, -len(kv[1])),
    )
    for src, ds in sorted_sources:
        count = len(ds)
        first_ts = (ds[-1].get("timestamp") or "")[:19]
        last_ts = (ds[0].get("timestamp") or "")[:19]
        if src in NOISE_SOURCES:
            blocks.append(
                f"── {src} (NOISE — {count} deltas, {first_ts} … {last_ts}) ──"
            )
            continue
        blocks.append(f"── {src} ({count} deltas, {first_ts} … {last_ts}) ──")
        for d in ds[:MAX_SAMPLES_PER_SOURCE]:
            did = (d.get("id") or "")[:12]
            ts = (d.get("timestamp") or "")[11:16]
            content = (d.get("content") or "").strip().replace("\n", " ")
            if len(content) > MAX_SAMPLE_CHARS:
                content = content[:MAX_SAMPLE_CHARS] + "…"
            tags_short = [
                t for t in (d.get("tags") or [])
                if t.startswith("kind:") or t.startswith("chat:")
            ][:3]
            tag_suffix = f" {tags_short}" if tags_short else ""
            blocks.append(f"  {did} {ts}{tag_suffix} {content}")
        if count > MAX_SAMPLES_PER_SOURCE:
            blocks.append(f"  …({count - MAX_SAMPLES_PER_SOURCE} more from this source)")
    return "\n".join(blocks)


async def run_level_1(
    *,
    start: datetime,
    hours: float,
    auto: bool,
) -> int:
    """One level-1 pass over a time window."""
    end = start + timedelta(hours=hours)
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = end.isoformat().replace("+00:00", "Z")
    print(f"=== topical agent (level 1) ===")
    print(f"window: {start_iso} … {end_iso} ({hours}h)\n")

    print("[pull] fetching deltas in window…")
    deltas = await _pull_window(start_iso=start_iso, end_iso=end_iso)
    if not deltas:
        print("[pull] window is empty.")
        return 0
    print(f"[pull] {len(deltas)} deltas across {len({d.get('source') for d in deltas})} sources")

    activity_block = _compress_window_by_source(deltas)
    valid_ids = {d.get("id") for d in deltas if d.get("id")}

    window_block = (
        f"start: {start_iso}\n"
        f"end:   {end_iso}\n"
        f"hours: {hours}\n"
        f"total deltas: {len(deltas)}"
    )

    prompt = L1_PROMPT.format(
        window_block=window_block,
        activity_block=activity_block[:18000],
    )

    print("[llm] composing clusters…")
    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="hard",
            max_tokens=8192,
            temperature=0.5,
            json_mode=True,
        )
    except Exception as e:
        print(f"[llm] crashed: {type(e).__name__}: {e}")
        return 2

    parsed = _parse_json(raw)
    if parsed is None:
        print(f"[llm] no parsable JSON; raw[:200]={raw[:200]!r}")
        return 2

    clusters = parsed.get("clusters") or []
    if not clusters:
        print("[llm] no clusters proposed for this window.")
        return 0

    print(f"[llm] {len(clusters)} clusters proposed.\n")

    session_tag = f"session:topical-l1-{uuid.uuid4().hex[:8]}"
    seed_label = f"window:{start_iso}..{end_iso}"
    written = 0
    for cluster in clusters:
        title = (cluster.get("title") or "").strip()
        summary = (cluster.get("summary") or "").strip()
        rationale = (cluster.get("rationale") or "").strip()
        from_ids = [
            x for x in (cluster.get("from_ids") or [])
            if isinstance(x, str) and x in valid_ids
        ]
        test_questions = [
            q for q in (cluster.get("test_questions") or []) if isinstance(q, str)
        ]
        if not (title and summary and from_ids):
            print(f"  [skip] incomplete cluster: title={title!r} from_count={len(from_ids)}")
            continue

        print(f"  · {title}  (from_ids={len(from_ids)})")
        if not auto:
            try:
                ans = input("    write proposal? [y/N/q] ").strip().lower()
            except EOFError:
                ans = ""
            if ans == "q":
                print("[stop] quit on user request.")
                break
            if ans not in ("y", "yes"):
                continue

        payload, tags = _build_proposal_payload(
            title=title, summary=summary, level=1, from_ids=from_ids,
            rationale=rationale, test_questions=test_questions,
            seed_label=seed_label,
        )
        try:
            new_id = await _post_proposal_draft(
                payload=payload, tags=tags,
                source="topical-agent", session_tag=session_tag,
            )
            print(f"    wrote proposal: {new_id}")
            written += 1
        except Exception as e:
            print(f"    write failed: {type(e).__name__}: {e}")

    print(f"\nDone. {written} proposals written, visible in dashboard for review.")
    return 0


# ─── level-2 producer ──────────────────────────────────────────────────


L2_PROMPT = """\
You are the second-level clustering layer of Fathom's mind. You take a
set of level-1 provenances (episodes) and propose level-2 provenance —
topics, threads that span multiple episodes.

A good level-2 names a recurring concern, a multi-day project, a
sustained line of inquiry. It groups episodes that share thematic
substance even when they happened at different times.

══ LEVEL-1 EPISODES ══
{episodes_block}

══ INSTRUCTIONS ══

Propose 1-N level-2 provenances grouping these episodes by topic /
project / sustained thread. Episodes that don't fit any meaningful
topic should be left unclustered — coverage is not the goal, accurate
thematic grouping is.

SIZE DISCIPLINE — 3-10 L1 episodes per L2 topic, plus optionally a
few base-moment stragglers if some belong to the topic but didn't
fit any episode. Bounded fan-out keeps the centroid sharp. If you'd
be naming a topic with 20+ episodes, that's two topics that need
splitting — propose them separately.

APPEND-ONLY — never propose to merge or replace existing L2 topics.
If you see better grouping in current episodes, propose a NEW L2
that overlaps the old one's coverage; the old one stays as
historical strata.

For each, return:
  · title: short, evocative, the name a human would search for
  · summary: 2-4 sentences naming the topic and its arc
  · from_ids: 3-10 level-1 provenance ids in this topic (plus a few
    base-moment stragglers if relevant)
  · rationale: one sentence — why these episodes form one topic
  · test_questions: 2-4 questions whose answer should surface this topic

Return JSON:
{{
  "topics": [
    {{
      "title": "...",
      "summary": "...",
      "from_ids": ["<l1_id>", ...],
      "rationale": "...",
      "test_questions": ["...", ...]
    }},
    ...
  ]
}}
"""


async def run_level_2(*, auto: bool) -> int:
    """One level-2 pass over all existing level-1 provenance."""
    print(f"=== topical agent (level 2) ===\n")

    print("[pull] loading all level-1 provenance…")
    try:
        l1_deltas = await delta_client.query(
            tags_include=["kind:provenance", "provenance-level:1"],
            limit=500,
        )
    except Exception as e:
        print(f"[pull] failed: {type(e).__name__}: {e}")
        return 2
    if not l1_deltas:
        print("[pull] no level-1 provenance found — run level-1 first.")
        return 0
    print(f"[pull] {len(l1_deltas)} level-1 episodes found.")

    episodes_block_lines: list[str] = []
    valid_ids: set[str] = set()
    for d in l1_deltas:
        did = (d.get("id") or "")[:24]
        if not did:
            continue
        valid_ids.add(did)
        valid_ids.add(d.get("id") or "")  # accept full ids too
        title = ""
        for t in d.get("tags") or []:
            if t.startswith("title:"):
                title = t.split(":", 1)[1]
                break
        ts = (d.get("timestamp") or "")[:10]
        content = (d.get("content") or "").strip().replace("\n", " ")
        if len(content) > 220:
            content = content[:220] + "…"
        episodes_block_lines.append(
            f"  {did} {ts} title={title!r}\n      {content}"
        )

    episodes_block = "\n".join(episodes_block_lines)

    prompt = L2_PROMPT.format(episodes_block=episodes_block[:24000])

    print("[llm] composing topic groupings…")
    try:
        # 16K — over hundreds of L1 episodes the output (titles +
        # summaries + from-id lists) doesn't fit in the 8K used for
        # single-fire witness work.
        raw = await loop_generate(
            prompt=prompt,
            tier="hard",
            max_tokens=16384,
            temperature=0.4,
            json_mode=True,
        )
    except Exception as e:
        print(f"[llm] crashed: {type(e).__name__}: {e}")
        return 2

    parsed = _parse_json(raw)
    if parsed is None:
        print(f"[llm] no parsable JSON; raw[:200]={raw[:200]!r}")
        return 2

    topics = parsed.get("topics") or []
    if not topics:
        print("[llm] no topic groupings proposed.")
        return 0

    print(f"[llm] {len(topics)} topics proposed.\n")
    session_tag = f"session:topical-l2-{uuid.uuid4().hex[:8]}"
    seed_label = f"l1-count:{len(l1_deltas)}"
    written = 0
    for topic in topics:
        title = (topic.get("title") or "").strip()
        summary = (topic.get("summary") or "").strip()
        rationale = (topic.get("rationale") or "").strip()
        from_ids = [
            x for x in (topic.get("from_ids") or [])
            if isinstance(x, str) and x in valid_ids
        ]
        test_questions = [
            q for q in (topic.get("test_questions") or []) if isinstance(q, str)
        ]
        if not (title and summary) or len(from_ids) < 2:
            print(f"  [skip] incomplete topic: title={title!r} from_count={len(from_ids)}")
            continue

        print(f"  · {title}  (l1_ids={len(from_ids)})")
        if not auto:
            try:
                ans = input("    write proposal? [y/N/q] ").strip().lower()
            except EOFError:
                ans = ""
            if ans == "q":
                print("[stop] quit on user request.")
                break
            if ans not in ("y", "yes"):
                continue

        payload, tags = _build_proposal_payload(
            title=title, summary=summary, level=2, from_ids=from_ids,
            rationale=rationale, test_questions=test_questions,
            seed_label=seed_label,
        )
        try:
            new_id = await _post_proposal_draft(
                payload=payload, tags=tags,
                source="topical-agent", session_tag=session_tag,
            )
            print(f"    wrote proposal: {new_id}")
            written += 1
        except Exception as e:
            print(f"    write failed: {type(e).__name__}: {e}")

    print(f"\nDone. {written} topic proposals written, visible in dashboard for review.")
    return 0


# ─── main ──────────────────────────────────────────────────────────────


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def _parse_iso(s: str) -> datetime:
    s = s.strip()
    # Accept Z suffix or explicit offset.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--level", type=int, choices=[1, 2], required=True,
        help="1 = window-based episodes; 2 = topic groupings over existing L1",
    )
    ap.add_argument(
        "--window-start", default="",
        help="ISO timestamp where the window begins (level 1 only)",
    )
    ap.add_argument(
        "--window-hours", type=float, default=6.0,
        help="window size in hours (level 1 only, default 6)",
    )
    ap.add_argument(
        "--auto", action="store_true",
        help="skip per-cluster confirmation; drop everything to the dashboard",
    )
    return ap.parse_args()


async def _main(args) -> int:
    if args.level == 1:
        if not args.window_start:
            print(
                "level-1 requires --window-start ISO timestamp (e.g. "
                "2026-04-06T00:00:00Z)",
                file=sys.stderr,
            )
            return 2
        try:
            start = _parse_iso(args.window_start)
        except Exception as e:
            print(f"--window-start parse failed: {e}", file=sys.stderr)
            return 2
        return await run_level_1(start=start, hours=args.window_hours, auto=args.auto)
    return await run_level_2(auto=args.auto)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse_args())))
