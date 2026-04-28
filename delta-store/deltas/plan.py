"""Compositional query plan executor.

Accepts a JSON query plan with named steps. Each step is one of:
  search, filter, intersect, union, diff, bridge, aggregate, chain,
  neighbors.

`neighbors` is the region primitive — for each delta in a referenced
step, fetch the temporally-surrounding deltas (default same source,
±30 minutes). Use when the load-bearing context of a hit is its
neighbors, not the hit alone.

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
)
from deltas.query import _noise_modifier, get_noise_centroid
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

        # Build response
        elapsed_ms = (time.monotonic() - t0) * 1000
        step_results: dict[str, StepResultDeltas | StepResultAggregate] = {}

        for step in steps:
            action = self._action_type(step)
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
                    "chain, neighbors"
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
        ):
            if getattr(step, action) is not None:
                return action
        return None

    def _get_refs(self, step: PlanStep, action: str) -> list[str]:
        if action in ("intersect", "union", "diff", "bridge"):
            return getattr(step, action, []) or []
        if action in ("aggregate", "chain", "neighbors"):
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
        for d in rows:
            base = d.get("distance")
            if base is None:
                continue
            d["distance"] = float(base) * _noise_modifier(
                d.get("content"), d.get("embedding") or [], centroid
            )
        # Stable sort by distance ascending; rows missing distance keep
        # their relative order at the end of the list.
        rows.sort(key=lambda d: (d.get("distance") is None, d.get("distance") or 0.0))
        return rows[:target_limit]


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO timestamp string to a timezone-aware datetime."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts).replace(tzinfo=UTC)
