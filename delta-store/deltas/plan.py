"""Compositional query plan executor.

Accepts a JSON query plan with named steps. Each step is one of:
  search, filter, intersect, union, diff, bridge, aggregate, chain,
  neighbors, timeline.

`neighbors` is the region primitive — for each delta in a referenced
step, fetch the temporally-surrounding deltas (default same source,
±30 minutes). Use when the load-bearing context of a hit is its
neighbors, not the hit alone.

`timeline` is the moment-reconstruction primitive — for each delta in
a referenced step, fetch ALL surrounding deltas (any source, ambient
included) and assemble them into chronological strips. Strips trim at
silences larger than `gap_minutes`, run-length collapse high-frequency
bursts (heartbeats, sysinfo) named in `collapse_sources`, and merge
when two anchors' windows are close enough to be the same moment.
Returns `StepResultTimelines`, not deltas.

Search / bridge / chain results pass through a noise rerank: over-
fetch by 2× + 10, multiply each row's distance by `_noise_modifier`
(short content + generic-ack-centroid alignment), sort, trim to the
requested limit. This keeps the compositional path symmetric with
shallow `/search`, which has applied the same suppression for a while.

Execution uses a hybrid approach: consecutive SQL-only steps compile
into a single CTE chain. Steps requiring Python (embedding text,
computing centroids from prior results) break the chain. The executor
flushes the CTE batch, does the Python work, then starts a new batch.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

import asyncpg
import numpy as np

from deltas.models import (
    AggBucket,
    DeltaSlim,
    PlanRequest,
    PlanResponse,
    PlanStep,
    StepResultAggregate,
    StepResultDeltas,
    StepResultTimelines,
    Timeline,
    TimelineDelta,
)
from deltas.query import _is_pure_noise, _noise_modifier, get_noise_centroid
from deltas.store import _format_ts, _vec_to_list

# Compositional steps that produce semantic distances — search/bridge/chain.
# These get noise-suppression rerank applied: over-fetch by a multiplier,
# bump distance for short / generic-noise-centroid-aligned content, sort,
# trim to the requested limit. Without this, deep recall surfaces the same
# trash the shallow path filters out (single-word acks, throwaway lines).
_NOISE_OVERFETCH = 2.0
_NOISE_OVERFETCH_FLOOR = 10  # always over-fetch at least this many extras
_NOISE_HARD_CAP = 2000  # don't over-fetch past what _exec_search caps anyway


class PlanExecutor:
    def __init__(self, pool: asyncpg.Pool, embed_fn: Callable[[str], list[float]]):
        self._pool = pool
        self._embed_fn = embed_fn

    async def execute(self, plan: PlanRequest) -> PlanResponse:
        t0 = time.monotonic()
        steps = plan.steps
        self._validate(steps)

        # Resolved results per step — keyed by step id
        resolved: dict[str, list[dict]] = {}
        # Embeddings computed for search steps — keyed by step id
        embeddings: dict[str, np.ndarray] = {}
        # Timelines produced by `timeline` steps — keyed by step id.
        # Each entry is a list of dicts: {id, t_start, t_end, anchor_ids,
        # deltas (list[TimelineDelta]), _raw_deltas (real deltas only,
        # for downstream chaining)}.
        timelines_by_step: dict[str, list[dict]] = {}
        warnings: list[str] = []

        for step in steps:
            action = self._action_type(step)

            if action == "search":
                emb = np.array(self._embed_fn(step.search), dtype=np.float32)
                embeddings[step.id] = emb
                rows = await self._exec_search(step, emb)
                resolved[step.id] = rows

            elif action == "filter":
                rows = await self._exec_filter(step)
                resolved[step.id] = rows

            elif action in ("intersect", "union", "diff"):
                refs = getattr(step, action)
                set_a = {d["id"] for d in resolved.get(refs[0], [])}
                set_b = {d["id"] for d in resolved.get(refs[1], [])}

                if action == "intersect":
                    keep = set_a & set_b
                elif action == "union":
                    keep = set_a | set_b
                else:
                    keep = set_a - set_b

                # Merge from both source sets, dedup by id
                merged = {}
                for ref_id in refs:
                    for d in resolved.get(ref_id, []):
                        if d["id"] in keep and d["id"] not in merged:
                            merged[d["id"]] = d

                rows = list(merged.values())[: step.limit]
                resolved[step.id] = rows

            elif action == "bridge":
                refs = step.bridge
                set_a = resolved.get(refs[0], [])
                set_b = resolved.get(refs[1], [])

                if not set_a or not set_b:
                    warnings.append(
                        f"step '{step.id}' skipped: input "
                        f"'{refs[0] if not set_a else refs[1]}' is empty"
                    )
                    resolved[step.id] = []
                    continue

                rows = await self._exec_bridge(step, set_a, set_b)
                resolved[step.id] = rows

            elif action == "aggregate":
                source_rows = resolved.get(step.aggregate, [])
                agg_result = self._exec_aggregate(step, source_rows)
                # Store raw for chaining but mark as aggregate
                resolved[step.id] = agg_result

            elif action == "chain":
                source_rows = resolved.get(step.chain, [])
                if not source_rows:
                    warnings.append(f"step '{step.id}' skipped: input '{step.chain}' is empty")
                    resolved[step.id] = []
                    continue

                # Compute centroid from source step's embeddings
                embs = [
                    d["embedding"]
                    for d in source_rows
                    if isinstance(d.get("embedding"), (list, np.ndarray))
                    and len(d.get("embedding", []))
                ]
                if not embs:
                    # Fall back: fetch embeddings from DB
                    embs = await self._fetch_embeddings([d["id"] for d in source_rows])

                if not embs:
                    warnings.append(f"step '{step.id}' skipped: no embeddings in '{step.chain}'")
                    resolved[step.id] = []
                    continue

                centroid = np.mean(embs, axis=0).astype(np.float32)
                norm = np.linalg.norm(centroid)
                if norm > 0:
                    centroid = centroid / norm
                embeddings[step.id] = centroid
                rows = await self._exec_search(step, centroid)
                resolved[step.id] = rows

            elif action == "neighbors":
                source_rows = resolved.get(step.neighbors, [])
                if not source_rows:
                    warnings.append(
                        f"step '{step.id}' skipped: input '{step.neighbors}' is empty"
                    )
                    resolved[step.id] = []
                    continue
                rows = await self._exec_neighbors(step, source_rows)
                resolved[step.id] = rows

            elif action == "timeline":
                source_rows = resolved.get(step.timeline, [])
                if not source_rows:
                    warnings.append(
                        f"step '{step.id}' skipped: input '{step.timeline}' is empty"
                    )
                    timelines_by_step[step.id] = []
                    resolved[step.id] = []
                    continue
                tls = await self._exec_timeline(step, source_rows)
                timelines_by_step[step.id] = tls
                # Also flatten real (non-collapsed) deltas into resolved so
                # downstream chain/bridge steps can still reference this
                # step. Collapsed virtual rows are skipped — they have no
                # embedding and aren't real lake rows.
                flat: list[dict] = []
                seen_ids: set[str] = set()
                for tl in tls:
                    for d in tl["_raw_deltas"]:
                        did = d.get("id")
                        if not did or did in seen_ids:
                            continue
                        seen_ids.add(did)
                        flat.append(d)
                resolved[step.id] = flat

        # Build response
        elapsed_ms = (time.monotonic() - t0) * 1000
        step_results: dict[
            str, StepResultDeltas | StepResultAggregate | StepResultTimelines
        ] = {}

        for step in steps:
            action = self._action_type(step)

            if action == "timeline":
                tls = timelines_by_step.get(step.id, [])
                count = sum(
                    1 for tl in tls for d in tl["deltas"] if d.kind != "collapsed"
                )
                step_results[step.id] = StepResultTimelines(
                    count=count,
                    timelines=[
                        Timeline(
                            id=tl["id"],
                            t_start=tl["t_start"],
                            t_end=tl["t_end"],
                            anchor_ids=tl["anchor_ids"],
                            deltas=tl["deltas"],
                        )
                        for tl in tls
                    ],
                )
                continue

            data = resolved.get(step.id, [])
            if action == "aggregate":
                # data is already a list of AggBucket-like dicts
                step_results[step.id] = StepResultAggregate(buckets=[AggBucket(**b) for b in data])
            else:
                deltas = [self._to_slim(d) for d in data]
                step_results[step.id] = StepResultDeltas(count=len(deltas), deltas=deltas)

        return PlanResponse(steps=step_results, timing_ms=round(elapsed_ms, 1), warnings=warnings)

    # ── Validation ───────────────────────────────────────────────────────

    def _validate(self, steps: list[PlanStep]) -> None:
        seen: set[str] = set()
        for step in steps:
            if step.id in seen:
                raise ValueError(f"Duplicate step id: '{step.id}'")
            seen.add(step.id)

            action = self._action_type(step)
            if action is None:
                raise ValueError(
                    f"Step '{step.id}' has no action — must set exactly one of: "
                    "search, filter, intersect, union, diff, bridge, aggregate, "
                    "chain, neighbors, timeline"
                )

            # Check references point to earlier steps
            refs = self._get_refs(step, action)
            for ref in refs:
                if ref not in seen:
                    raise ValueError(
                        f"Step '{step.id}' references '{ref}' which is not defined "
                        f"(or comes later in the plan)"
                    )

    def _action_type(self, step: PlanStep) -> str | None:
        for action in (
            "search",
            "filter",
            "intersect",
            "union",
            "diff",
            "bridge",
            "aggregate",
            "chain",
            "neighbors",
            "timeline",
        ):
            if getattr(step, action) is not None:
                return action
        return None

    def _get_refs(self, step: PlanStep, action: str) -> list[str]:
        if action in ("intersect", "union", "diff", "bridge"):
            return getattr(step, action, []) or []
        if action in ("aggregate", "chain", "neighbors", "timeline"):
            val = getattr(step, action, None)
            return [val] if val else []
        return []

    # ── Search execution ─────────────────────────────────────────────────

    async def _exec_search(self, step: PlanStep, embedding: np.ndarray) -> list[dict]:
        """Semantic search using pgvector <=> operator."""
        conditions = [
            "d.embedding IS NOT NULL",
            "(d.expires_at IS NULL OR d.expires_at > NOW())",
        ]
        params: list = [embedding]
        idx = 2

        radii = step.radii
        sem_radius = radii.semantic if radii else 1.0

        if step.tags_include:
            conditions.append(f"d.tags @> ${idx}")
            params.append(step.tags_include)
            idx += 1
        if step.tags_exclude:
            conditions.append(f"NOT (d.tags && ${idx})")
            params.append(step.tags_exclude)
            idx += 1
        if step.modality:
            conditions.append(f"d.modality = ${idx}")
            params.append(step.modality)
            idx += 1
        if step.source:
            conditions.append(f"d.source = ${idx}")
            params.append(step.source)
            idx += 1
        if step.time_start:
            conditions.append(f"d.timestamp >= ${idx}")
            params.append(_parse_ts(step.time_start))
            idx += 1
        if step.time_end:
            conditions.append(f"d.timestamp <= ${idx}")
            params.append(_parse_ts(step.time_end))
            idx += 1

        # Temporal radius as hours-from-now
        if radii and radii.temporal_hours is not None:
            conditions.append(f"d.timestamp >= ${idx}")
            params.append(
                datetime.now(UTC) - __import__("datetime").timedelta(hours=radii.temporal_hours)
            )
            idx += 1

        where = " AND ".join(conditions)
        target_limit = min(step.limit, _NOISE_HARD_CAP)
        # Over-fetch so the noise rerank can demote trash and still return
        # `target_limit` real items. The SQL cutoff is on raw distance —
        # without the over-fetch, a short ack at distance 0.3 would crowd
        # out a real hit at 0.45 even after the +20% length bump pushed
        # it to 0.36. Pulling 2× and reranking fixes that.
        fetch_limit = min(
            int(target_limit * _NOISE_OVERFETCH) + _NOISE_OVERFETCH_FLOOR,
            _NOISE_HARD_CAP,
        )
        params.append(fetch_limit)

        sql = f"""
            SELECT d.id, d.timestamp, d.modality, d.content, d.source, d.tags,
                   d.media_hash, d.expires_at, d.embedding,
                   (d.embedding <=> $1) AS distance
            FROM deltas d
            WHERE {where}
              AND (d.embedding <=> $1) < ${idx + 1}
            ORDER BY d.embedding <=> $1
            LIMIT ${idx}
        """
        params.append(float(sem_radius))

        rows = await self._pool.fetch(sql, *params)
        results = [self._row_to_dict(r) for r in rows]
        return self._apply_noise_rerank(results, target_limit)

    # ── Filter execution ─────────────────────────────────────────────────

    async def _exec_filter(self, step: PlanStep) -> list[dict]:
        """Structured query — no embedding needed."""
        conditions = ["(d.expires_at IS NULL OR d.expires_at > NOW())"]
        params: list = []
        idx = 1

        if step.tags_include:
            conditions.append(f"d.tags @> ${idx}")
            params.append(step.tags_include)
            idx += 1
        if step.tags_exclude:
            conditions.append(f"NOT (d.tags && ${idx})")
            params.append(step.tags_exclude)
            idx += 1
        if step.modality:
            conditions.append(f"d.modality = ${idx}")
            params.append(step.modality)
            idx += 1
        if step.source:
            conditions.append(f"d.source = ${idx}")
            params.append(step.source)
            idx += 1
        if step.time_start:
            conditions.append(f"d.timestamp >= ${idx}")
            params.append(_parse_ts(step.time_start))
            idx += 1
        if step.time_end:
            conditions.append(f"d.timestamp <= ${idx}")
            params.append(_parse_ts(step.time_end))
            idx += 1

        # filter dict can have arbitrary keys
        filter_spec = step.filter or {}
        if "time_start" in filter_spec:
            conditions.append(f"d.timestamp >= ${idx}")
            params.append(_parse_ts(filter_spec["time_start"]))
            idx += 1
        if "time_end" in filter_spec:
            conditions.append(f"d.timestamp <= ${idx}")
            params.append(_parse_ts(filter_spec["time_end"]))
            idx += 1
        if "tags_include" in filter_spec:
            conditions.append(f"d.tags @> ${idx}")
            params.append(filter_spec["tags_include"])
            idx += 1
        if "tags_exclude" in filter_spec:
            conditions.append(f"NOT (d.tags && ${idx})")
            params.append(filter_spec["tags_exclude"])
            idx += 1
        if "source" in filter_spec:
            conditions.append(f"d.source = ${idx}")
            params.append(filter_spec["source"])
            idx += 1
        if "modality" in filter_spec:
            conditions.append(f"d.modality = ${idx}")
            params.append(filter_spec["modality"])
            idx += 1

        where = " AND ".join(conditions)
        params.append(step.limit)

        sql = f"""
            SELECT d.id, d.timestamp, d.modality, d.content, d.source, d.tags,
                   d.media_hash, d.expires_at, d.embedding
            FROM deltas d
            WHERE {where}
            ORDER BY d.timestamp DESC
            LIMIT ${idx}
        """

        rows = await self._pool.fetch(sql, *params)
        return [self._row_to_dict(r) for r in rows]

    # ── Bridge execution ─────────────────────────────────────────────────

    async def _exec_bridge(
        self, step: PlanStep, set_a: list[dict], set_b: list[dict]
    ) -> list[dict]:
        """Find deltas independently close to both sets' centroids."""
        centroid_a = self._compute_centroid(set_a)
        centroid_b = self._compute_centroid(set_b)

        if centroid_a is None or centroid_b is None:
            return []

        # Exclude deltas already in either set
        exclude_ids = [d["id"] for d in set_a] + [d["id"] for d in set_b]

        target_limit = min(step.limit, 1000)
        fetch_limit = min(
            int(target_limit * _NOISE_OVERFETCH) + _NOISE_OVERFETCH_FLOOR,
            _NOISE_HARD_CAP,
        )

        sql = """
            SELECT d.id, d.timestamp, d.modality, d.content, d.source, d.tags,
                   d.media_hash, d.expires_at, d.embedding,
                   GREATEST(d.embedding <=> $1, d.embedding <=> $2) AS bridge_dist
            FROM deltas d
            WHERE d.embedding IS NOT NULL
              AND (d.expires_at IS NULL OR d.expires_at > NOW())
              AND d.id != ALL($3)
            ORDER BY GREATEST(d.embedding <=> $1, d.embedding <=> $2)
            LIMIT $4
        """

        rows = await self._pool.fetch(sql, centroid_a, centroid_b, exclude_ids, fetch_limit)
        results = []
        for r in rows:
            d = self._row_to_dict(r)
            d["distance"] = float(r["bridge_dist"])
            results.append(d)
        return self._apply_noise_rerank(results, target_limit)

    # ── Neighbors execution ──────────────────────────────────────────────

    async def _exec_neighbors(self, step: PlanStep, seeds: list[dict]) -> list[dict]:
        """For each seed delta, pull surrounding deltas within ±radius_minutes.

        One round-trip — uses a `LATERAL` subquery so each seed gets its own
        time-windowed slice ordered by absolute distance from the seed's
        timestamp, capped at `limit_per_seed`. Results are deduped (a delta
        adjacent to two seeds appears once) and capped at `step.limit`
        ordered by closeness to its nearest seed.

        `source_match=True` (default) restricts each window to the seed's
        own source — without this, a journal-entry seed pulls in every
        agent-heartbeat written within the radius, which is exactly the
        kind of substrate noise this primitive exists to avoid.
        """
        if not seeds:
            return []
        # Pre-build the seed table — id, timestamp (parsed), source.
        seed_rows: list[tuple[str, datetime, str]] = []
        for s in seeds:
            sid = s.get("id")
            ts_str = s.get("timestamp")
            src = s.get("source") or ""
            if not sid or not ts_str:
                continue
            try:
                ts = _parse_ts(ts_str)
            except Exception:
                continue
            seed_rows.append((sid, ts, src))
        if not seed_rows:
            return []

        seed_ids = [r[0] for r in seed_rows]
        seed_timestamps = [r[1] for r in seed_rows]
        seed_sources = [r[2] for r in seed_rows]

        radius_minutes = max(1, int(step.radius_minutes))
        per_seed = max(1, int(step.limit_per_seed))
        exclude_sources = step.exclude_sources or []
        source_match = bool(step.source_match)

        # asyncpg can pass parallel arrays through `unnest`. Each seed
        # contributes a row to the LATERAL — its window is the radius
        # around its timestamp, optionally constrained to its own source.
        sql = """
            WITH seeds AS (
                SELECT * FROM unnest($1::text[], $2::timestamptz[], $3::text[])
                AS s(seed_id, seed_ts, seed_src)
            )
            SELECT n.id, n.timestamp, n.modality, n.content, n.source, n.tags,
                   n.media_hash, n.expires_at, n.embedding,
                   EXTRACT(EPOCH FROM (n.timestamp - s.seed_ts)) AS gap_seconds
            FROM seeds s
            CROSS JOIN LATERAL (
                SELECT d.*
                FROM deltas d
                WHERE d.timestamp BETWEEN s.seed_ts - ($4 || ' minutes')::interval
                                      AND s.seed_ts + ($4 || ' minutes')::interval
                  AND (d.expires_at IS NULL OR d.expires_at > NOW())
                  AND d.id != s.seed_id
                  AND d.id != ALL($5)
                  AND ($6 = false OR d.source = s.seed_src)
                  AND ($7::text[] IS NULL OR NOT (d.source = ANY($7)))
                ORDER BY ABS(EXTRACT(EPOCH FROM (d.timestamp - s.seed_ts)))
                LIMIT $8
            ) n
        """
        rows = await self._pool.fetch(
            sql,
            seed_ids,
            seed_timestamps,
            seed_sources,
            str(radius_minutes),
            seed_ids,
            source_match,
            exclude_sources or None,
            per_seed,
        )

        # Dedupe — a delta adjacent to multiple seeds shows up once,
        # keyed to its smallest absolute gap.
        best: dict[str, tuple[float, dict]] = {}
        for r in rows:
            d = self._row_to_dict(r)
            gap = abs(float(r["gap_seconds"]))
            # Treat absolute time gap as the row's distance for downstream
            # rendering / valence rerank — closer-in-time = more relevant.
            d["distance"] = gap / max(radius_minutes * 60.0, 1.0)
            existing = best.get(d["id"])
            if existing is None or gap < existing[0]:
                best[d["id"]] = (gap, d)

        merged = [d for _, d in best.values()]
        merged.sort(key=lambda d: d.get("distance", 0.0))
        return merged[: step.limit]

    # ── Timeline execution ───────────────────────────────────────────────

    async def _exec_timeline(
        self, step: PlanStep, seeds: list[dict]
    ) -> list[dict]:
        """For each seed, build a chronological strip of surrounding deltas.

        Returns a list of timeline dicts ordered by t_start, where each
        dict has shape::

          {"id": "tl_0",
           "t_start": "ISO",
           "t_end": "ISO",
           "anchor_ids": [...],
           "deltas": [TimelineDelta, ...],
           "_raw_deltas": [dict, ...]}  # internal: real (non-collapsed)
                                        # source rows, for downstream chain

        Pipeline per seed:
          1. SQL fetch: all deltas within ±radius_minutes of the seed,
             any source (ambient texture comes along), excluding any
             explicit `exclude_sources`.
          2. Sort chronologically.
          3. Gap trim: from the seed outward, stop at any silence
             larger than `gap_minutes`. The silence is the boundary.
          4. Per-side cap: take at most `max_per_side` deltas each
             direction (after gap trim).
          5. Run-length collapse: consecutive same-source deltas in
             `collapse_sources` of length ≥2 fold into a single
             virtual `kind:collapsed` row.

        Then across seeds:
          6. Sort windows by t_start.
          7. Interval merge: any two windows whose ranges are within
             `merge_gap_seconds` of each other become one timeline.
             Anchor sets union; deltas dedupe by id (real anchor flag
             OR'd; collapsed runs from different windows that overlap
             get re-collapsed at merge time).
        """
        if not seeds:
            return []

        # Build seed rows (id, ts, src). Skip seeds without a usable ts.
        seed_rows: list[tuple[str, datetime, str]] = []
        for s in seeds:
            sid = s.get("id")
            ts_str = s.get("timestamp")
            src = s.get("source") or ""
            if not sid or not ts_str:
                continue
            try:
                ts = _parse_ts(ts_str)
            except Exception:
                continue
            seed_rows.append((sid, ts, src))
        if not seed_rows:
            return []

        seed_ids = [r[0] for r in seed_rows]
        seed_ts_by_id = {r[0]: r[1] for r in seed_rows}
        seed_id_set = set(seed_ids)

        radius_minutes = max(1, int(step.radius_minutes))
        max_per_side = max(1, int(step.max_per_side))
        gap_minutes = max(1, int(step.gap_minutes))
        merge_gap_seconds = max(0, int(step.merge_gap_seconds))
        collapse_sources = set(step.collapse_sources or [])
        exclude_sources = step.exclude_sources or []

        # LATERAL fetch: every row in every seed's window, with seed_id
        # carried through so we can group server-side. NOT source-matched
        # — ambient is the point of timelines. We always include the seed
        # itself (no `d.id != s.seed_id` exclusion); it's the anchor.
        sql = """
            WITH seeds AS (
                SELECT * FROM unnest($1::text[], $2::timestamptz[])
                AS s(seed_id, seed_ts)
            )
            SELECT s.seed_id,
                   d.id, d.timestamp, d.modality, d.content, d.source,
                   d.tags, d.media_hash, d.expires_at,
                   EXTRACT(EPOCH FROM (d.timestamp - s.seed_ts)) AS gap_seconds
            FROM seeds s
            CROSS JOIN LATERAL (
                SELECT *
                FROM deltas dd
                WHERE dd.timestamp BETWEEN s.seed_ts - ($3 || ' minutes')::interval
                                       AND s.seed_ts + ($3 || ' minutes')::interval
                  AND (dd.expires_at IS NULL OR dd.expires_at > NOW())
                  AND ($4::text[] IS NULL OR NOT (dd.source = ANY($4)))
                ORDER BY dd.timestamp ASC
            ) d
            ORDER BY s.seed_id, d.timestamp ASC
        """
        rows = await self._pool.fetch(
            sql,
            seed_ids,
            [seed_ts_by_id[sid] for sid in seed_ids],
            str(radius_minutes),
            exclude_sources or None,
        )

        # Group rows by their seed_id.
        by_seed: dict[str, list[dict]] = {sid: [] for sid in seed_ids}
        for r in rows:
            d = self._row_to_dict(r)
            by_seed.setdefault(r["seed_id"], []).append(d)

        # Per-seed window: gap-trim around the anchor, cap per side,
        # collapse high-freq bursts. Produce a window dict.
        windows: list[dict] = []
        gap_seconds_threshold = gap_minutes * 60.0
        for seed_id in seed_ids:
            ordered = by_seed.get(seed_id, [])
            if not ordered:
                # Anchor without any rows in window — can happen if the
                # anchor itself was deleted or expired; skip.
                continue
            # Locate the anchor in the ordered list. If multiple rows
            # share its id (shouldn't, but defensive), take the first.
            anchor_idx = next(
                (i for i, d in enumerate(ordered) if d.get("id") == seed_id),
                None,
            )
            if anchor_idx is None:
                # Anchor row was filtered (e.g. excluded source matched
                # the seed's own source). Use the timestamp-nearest row.
                anchor_idx = self._nearest_index(ordered, seed_ts_by_id[seed_id])
                if anchor_idx is None:
                    continue

            trimmed = self._gap_trim(
                ordered,
                anchor_idx=anchor_idx,
                gap_seconds=gap_seconds_threshold,
                max_per_side=max_per_side,
            )
            collapsed = self._collapse_runs(
                self._collapse_same_second_bursts(
                    trimmed, protected_ids={seed_id}
                ),
                collapse_sources,
                protected_ids={seed_id},
            )
            anchor_set = {seed_id}
            t_start = trimmed[0]["timestamp"]
            t_end = trimmed[-1]["timestamp"]
            windows.append(
                {
                    "anchor_ids": anchor_set,
                    "t_start": t_start,
                    "t_end": t_end,
                    "rows": collapsed,         # mix of real dicts + collapsed virtuals
                    "_raw_rows": trimmed,      # real dicts only, pre-collapse
                }
            )

        if not windows:
            return []

        # Sort by t_start, merge overlapping or near-adjacent windows.
        windows.sort(key=lambda w: _parse_ts(w["t_start"]))
        merged: list[dict] = []
        for w in windows:
            if not merged:
                merged.append(w)
                continue
            prev = merged[-1]
            prev_end_dt = _parse_ts(prev["t_end"])
            this_start_dt = _parse_ts(w["t_start"])
            gap = (this_start_dt - prev_end_dt).total_seconds()
            if gap <= merge_gap_seconds:
                # Merge: union anchors, union raw rows (dedup by id),
                # rebuild collapsed list from the unioned raws.
                merged_anchors = prev["anchor_ids"] | w["anchor_ids"]
                seen: set[str] = set()
                merged_raws: list[dict] = []
                for d in prev["_raw_rows"] + w["_raw_rows"]:
                    did = d.get("id")
                    if not did or did in seen:
                        continue
                    seen.add(did)
                    merged_raws.append(d)
                merged_raws.sort(key=lambda d: _parse_ts(d["timestamp"]))
                prev["anchor_ids"] = merged_anchors
                prev["t_start"] = merged_raws[0]["timestamp"]
                prev["t_end"] = merged_raws[-1]["timestamp"]
                prev["_raw_rows"] = merged_raws
                merged_anchor_set = set(prev["anchor_ids"]) | set(w["anchor_ids"])
                prev["rows"] = self._collapse_runs(
                    self._collapse_same_second_bursts(
                        merged_raws, protected_ids=merged_anchor_set
                    ),
                    collapse_sources,
                    protected_ids=merged_anchor_set,
                )
            else:
                merged.append(w)

        # Convert to the public shape: TimelineDelta list with is_anchor
        # marked, plus _raw_deltas for downstream chain reuse.
        out: list[dict] = []
        for i, w in enumerate(merged):
            anchor_ids = w["anchor_ids"]
            tdeltas: list[TimelineDelta] = []
            for row in w["rows"]:
                if row.get("kind") == "collapsed":
                    tdeltas.append(
                        TimelineDelta(
                            id=row["id"],
                            timestamp=row["t_start"],
                            modality="text",
                            content=row.get("content", ""),
                            source=row["source"],
                            tags=row.get("tags", []),
                            kind="collapsed",
                            count=row.get("count"),
                            t_start=row.get("t_start"),
                            t_end=row.get("t_end"),
                        )
                    )
                else:
                    is_anchor = row.get("id") in anchor_ids
                    tdeltas.append(
                        TimelineDelta(
                            id=row["id"],
                            timestamp=row["timestamp"],
                            modality=row.get("modality", "text"),
                            content=row.get("content", ""),
                            source=row.get("source", ""),
                            tags=row.get("tags", []),
                            media_hash=row.get("media_hash"),
                            expires_at=row.get("expires_at"),
                            is_anchor=is_anchor,
                        )
                    )
            out.append(
                {
                    "id": f"tl_{i}",
                    "t_start": w["t_start"],
                    "t_end": w["t_end"],
                    "anchor_ids": sorted(anchor_ids & seed_id_set),
                    "deltas": tdeltas,
                    "_raw_deltas": w["_raw_rows"],
                }
            )
        return out[: step.limit] if step.limit else out

    @staticmethod
    def _nearest_index(rows: list[dict], target_ts: datetime) -> int | None:
        if not rows:
            return None
        best_i = 0
        best_gap = float("inf")
        for i, d in enumerate(rows):
            try:
                ts = _parse_ts(d["timestamp"])
            except Exception:
                continue
            gap = abs((ts - target_ts).total_seconds())
            if gap < best_gap:
                best_gap = gap
                best_i = i
        return best_i

    @staticmethod
    def _gap_trim(
        ordered: list[dict],
        *,
        anchor_idx: int,
        gap_seconds: float,
        max_per_side: int,
    ) -> list[dict]:
        """Walk outward from anchor; stop at any neighbor-to-neighbor gap
        larger than `gap_seconds`. Then cap per side."""
        n = len(ordered)
        if n == 0:
            return []
        # Walk left.
        left_bound = anchor_idx
        for i in range(anchor_idx - 1, -1, -1):
            try:
                t_here = _parse_ts(ordered[i]["timestamp"])
                t_next = _parse_ts(ordered[i + 1]["timestamp"])
            except Exception:
                break
            gap = (t_next - t_here).total_seconds()
            if gap > gap_seconds:
                break
            left_bound = i
        # Walk right.
        right_bound = anchor_idx
        for i in range(anchor_idx + 1, n):
            try:
                t_here = _parse_ts(ordered[i]["timestamp"])
                t_prev = _parse_ts(ordered[i - 1]["timestamp"])
            except Exception:
                break
            gap = (t_here - t_prev).total_seconds()
            if gap > gap_seconds:
                break
            right_bound = i
        # Apply per-side cap.
        left_bound = max(left_bound, anchor_idx - max_per_side)
        right_bound = min(right_bound, anchor_idx + max_per_side)
        return ordered[left_bound : right_bound + 1]

    @staticmethod
    def _collapse_same_second_bursts(
        rows: list[dict],
        *,
        protected_ids: set[str] | None = None,
    ) -> list[dict]:
        """Fold runs of ≥2 same-source deltas sharing a second-resolution
        timestamp into a single virtual ``kind:collapsed`` row.

        Catches the import-artifact case: a vault chunker that emits one
        delta per markdown header lands many deltas at the same import
        second. None of those sources belong on the heartbeat-style
        collapse list, but they're still structurally bursty in a way
        that drowns conversational signal in a strip. Distinct-source
        deltas at the same second do NOT collapse — that's just
        coincident timing across the substrate, not a chunking artifact.

        ``protected_ids`` (anchors): a run that contains any protected id
        is emitted as-is, NOT collapsed. Anchors are the load-bearing
        signal of a strip; folding them into a `× N` count erases the
        match. The same-second neighbors stay alongside the anchor.
        """
        if not rows:
            return list(rows)
        protected = protected_ids or set()
        out: list[dict] = []
        i = 0
        n = len(rows)
        run_id = 1000  # offset to avoid id-collision with _collapse_runs
        while i < n:
            d = rows[i]
            src = d.get("source", "")
            ts = d.get("timestamp", "") or ""
            sec = ts[:19]  # "YYYY-MM-DDTHH:MM:SS"
            j = i + 1
            while j < n:
                nxt = rows[j]
                nxt_ts = nxt.get("timestamp", "") or ""
                if nxt.get("source", "") != src or nxt_ts[:19] != sec:
                    break
                j += 1
            run_count = j - i
            run_has_anchor = any(rows[k].get("id") in protected for k in range(i, j))
            if run_count >= 2 and src and sec and not run_has_anchor:
                first = rows[i]
                last = rows[j - 1]
                out.append(
                    {
                        "id": f"_samesec_{run_id}",
                        "kind": "collapsed",
                        "source": src,
                        "count": run_count,
                        "t_start": first["timestamp"],
                        "t_end": last["timestamp"],
                        "content": f"[{src} × {run_count} at {sec[11:19]}]",
                        "tags": [],
                    }
                )
                run_id += 1
                i = j
                continue
            out.append(d)
            i += 1
        return out

    @staticmethod
    def _collapse_runs(
        rows: list[dict],
        collapse_sources: set[str],
        *,
        protected_ids: set[str] | None = None,
    ) -> list[dict]:
        """Fold runs of ≥2 same-source deltas in `collapse_sources` into a
        single virtual `kind:collapsed` row. Real rows in other sources
        pass through unchanged.

        ``protected_ids`` (anchors): runs containing a protected id pass
        through uncollapsed, so anchors survive even if their source is
        somehow on the collapse list."""
        if not collapse_sources or not rows:
            return list(rows)
        protected = protected_ids or set()
        out: list[dict] = []
        i = 0
        n = len(rows)
        run_id = 0
        while i < n:
            d = rows[i]
            src = d.get("source", "")
            if src in collapse_sources:
                j = i + 1
                while j < n and rows[j].get("source") == src:
                    j += 1
                run_count = j - i
                run_has_anchor = any(rows[k].get("id") in protected for k in range(i, j))
                if run_count >= 2 and not run_has_anchor:
                    first = rows[i]
                    last = rows[j - 1]
                    # Already-collapsed dicts (from _collapse_same_second_bursts
                    # running first) carry t_start/t_end instead of timestamp.
                    # Fall back so a same-source run of those folds cleanly.
                    out.append(
                        {
                            "id": f"_collapsed_{run_id}",
                            "kind": "collapsed",
                            "source": src,
                            "count": run_count,
                            "t_start": first.get("timestamp") or first.get("t_start"),
                            "t_end": last.get("timestamp") or last.get("t_end"),
                            "content": f"[{src} × {run_count}]",
                            "tags": [],
                        }
                    )
                    run_id += 1
                    i = j
                    continue
            out.append(d)
            i += 1
        return out

    # ── Aggregate execution ──────────────────────────────────────────────

    def _exec_aggregate(self, step: PlanStep, source_rows: list[dict]) -> list[dict]:
        """Group rows by time bucket, tag, or source and compute metrics."""
        group_by = step.group_by or "week"

        buckets: dict[str, list[dict]] = {}

        for d in source_rows:
            if group_by == "tag":
                tags = d.get("tags", [])
                for tag in tags:
                    buckets.setdefault(tag, []).append(d)
            elif group_by == "source":
                buckets.setdefault(d.get("source", "unknown"), []).append(d)
            else:
                # Time-based bucketing
                ts_str = d.get("timestamp", "")
                try:
                    dt = _parse_ts(ts_str)
                except Exception:
                    continue

                if group_by == "day":
                    key = dt.strftime("%Y-%m-%d")
                elif group_by == "month":
                    key = dt.strftime("%Y-%m")
                elif group_by == "hour":
                    key = dt.strftime("%Y-%m-%d %H:00")
                else:  # week
                    key = f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"

                buckets.setdefault(key, []).append(d)

        result = []
        for key in sorted(buckets.keys()):
            items = buckets[key]
            entry = {
                "bucket": key,
                "count": len(items),
                "delta_ids": [d["id"] for d in items],
            }
            result.append(entry)

        return result

    # ── Helpers ───────────────────────────────────────────────────────────

    def _compute_centroid(self, rows: list[dict]) -> np.ndarray | None:
        """Compute normalized centroid from rows with embeddings."""
        embs = []
        for d in rows:
            e = d.get("embedding")
            if isinstance(e, np.ndarray):
                embs.append(e)
            elif isinstance(e, list) and e:
                embs.append(np.array(e, dtype=np.float32))
        if not embs:
            return None
        arr = np.stack(embs)
        centroid = arr.mean(axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm
        return centroid

    async def _fetch_embeddings(self, ids: list[str]) -> list[np.ndarray]:
        """Fetch embeddings for a list of delta IDs from the database."""
        if not ids:
            return []
        rows = await self._pool.fetch(
            "SELECT id, embedding FROM deltas WHERE id = ANY($1) AND embedding IS NOT NULL",
            ids,
        )
        return [np.array(_vec_to_list(r["embedding"]), dtype=np.float32) for r in rows]

    def _row_to_dict(self, r: asyncpg.Record) -> dict:
        """Convert a database row to a dict."""
        d = {
            "id": r["id"],
            "timestamp": _format_ts(r["timestamp"]),
            "modality": r["modality"],
            "content": r["content"],
            "source": r["source"],
            "tags": list(r["tags"]) if r["tags"] else [],
        }
        if "embedding" in r and r["embedding"] is not None:
            d["embedding"] = _vec_to_list(r["embedding"])
        if r["media_hash"]:
            d["media_hash"] = r["media_hash"]
        if r["expires_at"]:
            d["expires_at"] = _format_ts(r["expires_at"])
        if "distance" in r:
            d["distance"] = float(r["distance"])
        if "bridge_dist" in r:
            d["distance"] = float(r["bridge_dist"])
        return d

    def _to_slim(self, d: dict) -> DeltaSlim:
        """Convert a dict to a DeltaSlim model."""
        return DeltaSlim(
            id=d["id"],
            timestamp=d["timestamp"],
            modality=d["modality"],
            content=d["content"],
            source=d["source"],
            tags=d.get("tags", []),
            media_hash=d.get("media_hash"),
            expires_at=d.get("expires_at"),
            distance=d.get("distance"),
        )

    def _apply_noise_rerank(self, rows: list[dict], target_limit: int) -> list[dict]:
        """Apply the noise modifier to each row's distance and re-sort.

        Mirrors what `query.py:QueryEngine.search` does on the shallow
        path, so deep recall doesn't surface trash that shallow recall
        already filters out. Trims to `target_limit` after sorting.
        Soft-fails to length-only on missing centroid; tolerates rows
        without distance (no-op).
        """
        if not rows:
            return rows
        centroid = get_noise_centroid()
        kept: list[dict] = []
        for d in rows:
            if _is_pure_noise(d.get("content"), d.get("embedding") or [], centroid):
                continue
            base = d.get("distance")
            if base is not None:
                d["distance"] = float(base) * _noise_modifier(
                    d.get("content"), d.get("embedding") or [], centroid
                )
            kept.append(d)
        # Stable sort by distance ascending; rows missing distance keep
        # their relative order at the end of the list.
        kept.sort(key=lambda d: (d.get("distance") is None, d.get("distance") or 0.0))
        return kept[:target_limit]


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO timestamp string to a UTC-aware datetime.

    Naive inputs are assumed UTC (lake timestamps are server-assigned UTC).
    Inputs carrying a non-UTC offset are *converted* to UTC, not relabelled
    — `.replace(tzinfo=UTC)` would silently shift the wallclock by the
    offset, which surfaces as time-window queries returning the wrong
    bucket for any planner-supplied non-UTC bound.
    """
    ts = ts.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
