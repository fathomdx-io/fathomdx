#!/usr/bin/env python3
"""Smoke parity — replay recent production user-messages through the harness.

Migration acceptance gate 1 from `docs/specs/harness-prd.md`: confirm
the harness handles representative recent fires at acceptable latency
and produces sensible cards. Not a strict reproduction test (the prov
lake is a separate substrate and the harness deliberates differently
each fire), just a "does it cope" check.

Pulls N recent (user, fathom) message pairs from the production lake
(default port 4246, no auth — localhost-only), replays each user
message through run_harness against whatever lake the script's
delta_client points at, captures latency + the resulting card, and
emits a markdown report + JSON metrics.

Two-phase usage (recommended) — the prov container can't reach the
host's 127.0.0.1:4246 (prod delta-store is loopback-only), so split
the fetch and the replay:

    # Phase 1: on the host, pull pairs from prod lake.
    python scripts/smoke_parity.py fetch --n 10 --pairs-file /tmp/pairs.json

    # Phase 2: copy the pairs file into the prov container, replay
    # there (where the LLM provider env + a reachable delta-store live).
    podman cp /tmp/pairs.json fathom-prov_api_1:/tmp/pairs.json
    podman exec fathom-prov_api_1 python /app/scripts/smoke_parity.py \\
        replay --pairs-file /tmp/pairs.json --out /tmp/smoke-parity.md
    podman cp fathom-prov_api_1:/tmp/smoke-parity.md ./

Single-phase usage (when both lakes are reachable from the same shell):

    FATHOM_DELTA_STORE_URL=http://localhost:4346 \\
    python scripts/smoke_parity.py run --n 10 \\
        --out smoke-parity-$(date +%Y%m%d-%H%M%S).md

The script never writes to the production lake; the read against the
baseline URL is a plain GET.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_BASELINE_URL = "http://localhost:4246"
DEFAULT_N = 10
DEFAULT_OUT = "smoke-parity.md"
PAIR_LIMIT_MULTIPLIER = 8  # pull 8N user msgs to find N usable pairs


async def fetch_pairs(baseline_url: str, n: int) -> list[dict]:
    """Pull recent user messages from the baseline lake, attach the next
    fathom message in the same loop-chat:<id> session as the baseline
    response. Returns up to `n` pairs ordered most-recent-first.

    A pair is usable when:
      - the user message has a loop-chat:<id> tag
      - a fathom message in the same session arrives within 5 minutes
      - the user content is non-empty

    The 8x over-fetch lets us drop pairs that don't satisfy the
    fathom-followed-by check without having to paginate.
    """
    pairs: list[dict] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Pull a bunch of user messages, newest first.
        r = await client.get(
            f"{baseline_url}/deltas",
            params={
                "tags_include": "participant:user",
                "limit": n * PAIR_LIMIT_MULTIPLIER,
            },
        )
        r.raise_for_status()
        users = r.json()

        # Group by session, then sort within session so we can find the
        # immediate next fathom reply.
        sessions: dict[str, list[dict]] = defaultdict(list)
        for u in users:
            session = next(
                (t for t in u.get("tags", []) if t.startswith("loop-chat:")),
                None,
            )
            if session:
                sessions[session].append(u)

        # For each session, fetch the full conversation in one call so we
        # can pair user→fathom by adjacency.
        for session_tag, _user_msgs in sessions.items():
            if len(pairs) >= n:
                break
            r = await client.get(
                f"{baseline_url}/deltas",
                params={"tags_include": session_tag, "limit": 100},
            )
            if r.status_code != 200:
                continue
            convo = sorted(r.json(), key=lambda d: d.get("timestamp", ""))
            for i, msg in enumerate(convo):
                if "participant:user" not in msg.get("tags", []):
                    continue
                content = (msg.get("content") or "").strip()
                if not content:
                    continue
                # Find next fathom reply in the conversation (within 5 min).
                reply = None
                for cand in convo[i + 1 :]:
                    if "participant:fathom" in cand.get("tags", []):
                        reply = cand
                        break
                if reply is None:
                    continue
                pairs.append(
                    {
                        "user_id": msg.get("id"),
                        "user_ts": msg.get("timestamp"),
                        "user_content": content,
                        "session_tag": session_tag,
                        "fathom_reply_id": reply.get("id"),
                        "fathom_reply_ts": reply.get("timestamp"),
                        "fathom_reply_content": (reply.get("content") or "").strip(),
                    }
                )
                if len(pairs) >= n:
                    break

    return pairs[:n]


def _import_harness():
    """Import the harness lazily — these imports are heavy and the fetch
    phase has no business pulling them in."""
    from api import standpoint as standpoint_mod
    from api.loop.harness import run_harness
    from api.loop.intents import CONVO_TAG
    from api.loop.puddle import puddle

    return {
        "standpoint_mod": standpoint_mod,
        "run_harness": run_harness,
        "CONVO_TAG": CONVO_TAG,
        "puddle": puddle,
    }


async def fire_one(pair: dict, h: dict) -> dict:
    """Replay one user message through the harness; return latency +
    addressed cards + any error."""
    session_tag = f"session:smoke-parity-{uuid.uuid4().hex[:8]}"
    question = pair["user_content"]

    record = {
        "user_id": pair["user_id"],
        "user_content": question,
        "user_ts": pair["user_ts"],
        "baseline_reply": pair["fathom_reply_content"],
        "session_tag": session_tag,
        "harness_cards": [],
        "addressed_intent_ids": [],
        "duration_seconds": None,
        "error": None,
    }

    # Seed the intent into the prov puddle.
    try:
        intent = await h["puddle"].write(
            content=question,
            tags=[h["CONVO_TAG"], "intent", "kind:question", "smoke-parity"],
            source="smoke-parity",
            ttl_seconds=60 * 60,
        )
    except Exception as e:
        record["error"] = f"seed_intent: {type(e).__name__}: {e}"
        return record

    # Phase 9: production worker.py no longer pre-seeds recall — the
    # harness elects search via its `semantic` tool when needed.
    # Smoke-parity mirrors that shape.

    try:
        standpoint = await h["standpoint_mod"].current(session_tag=session_tag)
    except Exception as e:
        record["standpoint_error"] = f"{type(e).__name__}: {e}"
        standpoint = None

    t0 = time.perf_counter()
    try:
        addressed = await h["run_harness"](
            session_tag=session_tag,
            pending=[intent],
            standpoint=standpoint,
        )
        record["addressed_intent_ids"] = list(addressed or [])
    except Exception as e:
        record["error"] = f"run_harness: {type(e).__name__}: {e}"
        return record
    finally:
        record["duration_seconds"] = round(time.perf_counter() - t0, 2)

    # Pull the resulting cards from the puddle scoped to our session.
    try:
        cards = h["puddle"].query(tags_include=[session_tag, "feed-card"], limit=5)
        for c in cards or []:
            try:
                payload = json.loads(c.get("content") or "{}")
            except Exception:
                payload = {"body": c.get("content")}
            record["harness_cards"].append(
                {
                    "route": payload.get("route"),
                    "kicker": payload.get("kicker"),
                    "title": payload.get("title"),
                    "body": payload.get("body"),
                    "tail": payload.get("tail"),
                }
            )
    except Exception as e:
        record["card_query_error"] = f"{type(e).__name__}: {e}"

    return record


def render_markdown(records: list[dict], pairs: list[dict]) -> str:
    """Side-by-side markdown report. One section per fire."""
    lines: list[str] = []
    lines.append("# Smoke parity — recent production fires through the harness")
    lines.append("")
    lines.append(f"Pairs sampled: **{len(pairs)}**, fires completed: **{len(records)}**.")
    lines.append("")

    # Latency summary.
    durations = [r["duration_seconds"] for r in records if r["duration_seconds"] is not None]
    if durations:
        durations_sorted = sorted(durations)
        p50 = durations_sorted[len(durations_sorted) // 2]
        p_max = durations_sorted[-1]
        n_under_30 = sum(1 for d in durations if d < 30)
        n_under_90 = sum(1 for d in durations if d < 90)
        lines.append("## Latency")
        lines.append("")
        lines.append(f"- p50 = **{p50:.2f}s**, max = **{p_max:.2f}s**")
        lines.append(
            f"- under PRD targets — typical 30s: **{n_under_30}/{len(durations)}**, "
            f"synthesis cap 90s: **{n_under_90}/{len(durations)}**"
        )
        lines.append("")

    # Outcome summary.
    n_carded = sum(1 for r in records if r["harness_cards"])
    n_addressed = sum(1 for r in records if r["addressed_intent_ids"])
    n_errored = sum(1 for r in records if r["error"])
    lines.append("## Outcomes")
    lines.append("")
    lines.append(f"- produced ≥1 card: **{n_carded}/{len(records)}**")
    lines.append(f"- addressed the intent: **{n_addressed}/{len(records)}**")
    lines.append(f"- errored before respond: **{n_errored}/{len(records)}**")
    lines.append("")

    # Per-fire detail.
    lines.append("## Per fire")
    lines.append("")
    for i, r in enumerate(records, 1):
        lines.append(f"### {i}. `{r['user_id']}` — {r['user_ts']}")
        lines.append("")
        lines.append(f"- session: `{r['session_tag']}`")
        lines.append(f"- duration: **{r['duration_seconds']}s**")
        if r.get("error"):
            lines.append(f"- **error**: `{r['error']}`")
        lines.append(f"- addressed: `{r['addressed_intent_ids']}`")
        lines.append("")
        lines.append("**User:**")
        lines.append("")
        lines.append(f"> {r['user_content']}")
        lines.append("")
        lines.append("**Production witness response (baseline):**")
        lines.append("")
        baseline = (r["baseline_reply"] or "(none)").splitlines()
        for line in baseline[:20]:
            lines.append(f"> {line}")
        if len(baseline) > 20:
            lines.append(f"> ...({len(baseline) - 20} more lines)")
        lines.append("")
        lines.append("**Harness cards:**")
        lines.append("")
        if not r["harness_cards"]:
            lines.append("> (no cards produced)")
        for c in r["harness_cards"]:
            lines.append(f"- route: `{c.get('route')}`, kicker: `{c.get('kicker')}`")
            if c.get("title"):
                lines.append(f"  - title: {c['title']}")
            body = (c.get("body") or "").splitlines()
            if body:
                lines.append("  - body:")
                for line in body[:20]:
                    lines.append(f"    > {line}")
                if len(body) > 20:
                    lines.append(f"    > ...({len(body) - 20} more lines)")
            if c.get("tail"):
                lines.append(f"  - tail: {c['tail']}")
        lines.append("")
    return "\n".join(lines)


async def _do_fetch(args: argparse.Namespace) -> int:
    print(f"[smoke-parity] fetching {args.n} pairs from {args.baseline_url}")
    pairs = await fetch_pairs(args.baseline_url, args.n)
    print(f"[smoke-parity] got {len(pairs)} usable pairs")
    if not pairs:
        print("[smoke-parity] no pairs collected — aborting")
        return 1
    for i, p in enumerate(pairs, 1):
        preview = p["user_content"][:90].replace("\n", " ")
        print(f"  {i:2d}. {p['user_ts'][:19]} | {preview}")
    Path(args.pairs_file).write_text(
        json.dumps(pairs, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[smoke-parity] wrote pairs → {args.pairs_file}")
    return 0


async def _do_replay(args: argparse.Namespace) -> int:
    pairs = json.loads(Path(args.pairs_file).read_text(encoding="utf-8"))
    if not isinstance(pairs, list) or not pairs:
        print(f"[smoke-parity] pairs file {args.pairs_file} empty or malformed")
        return 1
    print(f"[smoke-parity] loaded {len(pairs)} pairs from {args.pairs_file}")

    h = _import_harness()
    delta_store = os.environ.get("FATHOM_DELTA_STORE_URL", "(unset)")
    print(f"[smoke-parity] firing harness — FATHOM_DELTA_STORE_URL={delta_store}")

    records: list[dict] = []
    for i, pair in enumerate(pairs, 1):
        print(f"\n[smoke-parity] fire {i}/{len(pairs)}: {pair['user_content'][:60]!r}")
        rec = await fire_one(pair, h)
        print(
            f"  duration={rec['duration_seconds']}s "
            f"addressed={len(rec['addressed_intent_ids'])} "
            f"cards={len(rec['harness_cards'])}"
            f"{' err=' + rec['error'] if rec['error'] else ''}"
        )
        records.append(rec)

    md = render_markdown(records, pairs)
    Path(args.out).write_text(md, encoding="utf-8")
    print(f"\n[smoke-parity] wrote markdown report → {args.out}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"pairs": pairs, "records": records}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[smoke-parity] wrote JSON metrics → {args.json_out}")
    return 0


async def _do_run(args: argparse.Namespace) -> int:
    """Single-phase: fetch + replay in one shell. Use only when both
    the baseline lake and the harness's delta-store are reachable from
    here AND the LLM provider creds are available."""
    print(f"[smoke-parity] fetching {args.n} pairs from {args.baseline_url}")
    pairs = await fetch_pairs(args.baseline_url, args.n)
    print(f"[smoke-parity] got {len(pairs)} usable pairs")
    if not pairs:
        print("[smoke-parity] no pairs collected — aborting")
        return 1
    for i, p in enumerate(pairs, 1):
        preview = p["user_content"][:90].replace("\n", " ")
        print(f"  {i:2d}. {p['user_ts'][:19]} | {preview}")

    h = _import_harness()
    delta_store = os.environ.get("FATHOM_DELTA_STORE_URL", "(unset)")
    print(f"\n[smoke-parity] firing harness — FATHOM_DELTA_STORE_URL={delta_store}")
    if delta_store == args.baseline_url:
        print(
            "  WARNING: FATHOM_DELTA_STORE_URL == baseline — harness writes will "
            "land in the production lake. Re-aim at the prov stack."
        )

    records: list[dict] = []
    for i, pair in enumerate(pairs, 1):
        print(f"\n[smoke-parity] fire {i}/{len(pairs)}: {pair['user_content'][:60]!r}")
        rec = await fire_one(pair, h)
        print(
            f"  duration={rec['duration_seconds']}s "
            f"addressed={len(rec['addressed_intent_ids'])} "
            f"cards={len(rec['harness_cards'])}"
            f"{' err=' + rec['error'] if rec['error'] else ''}"
        )
        records.append(rec)

    md = render_markdown(records, pairs)
    Path(args.out).write_text(md, encoding="utf-8")
    print(f"\n[smoke-parity] wrote markdown report → {args.out}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"pairs": pairs, "records": records}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[smoke-parity] wrote JSON metrics → {args.json_out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="Pull pairs from the production lake to JSON.")
    p_fetch.add_argument("--baseline-url", default=DEFAULT_BASELINE_URL)
    p_fetch.add_argument("--n", type=int, default=DEFAULT_N)
    p_fetch.add_argument("--pairs-file", required=True)

    p_replay = sub.add_parser("replay", help="Replay pairs through the harness; write report.")
    p_replay.add_argument("--pairs-file", required=True)
    p_replay.add_argument("--out", default=DEFAULT_OUT)
    p_replay.add_argument("--json-out", default=None)

    p_run = sub.add_parser("run", help="Fetch + replay in one shell (rarely usable).")
    p_run.add_argument("--baseline-url", default=DEFAULT_BASELINE_URL)
    p_run.add_argument("--n", type=int, default=DEFAULT_N)
    p_run.add_argument("--out", default=DEFAULT_OUT)
    p_run.add_argument("--json-out", default=None)

    args = parser.parse_args()
    if args.cmd == "fetch":
        return asyncio.run(_do_fetch(args))
    if args.cmd == "replay":
        return asyncio.run(_do_replay(args))
    return asyncio.run(_do_run(args))


if __name__ == "__main__":
    sys.exit(main())
