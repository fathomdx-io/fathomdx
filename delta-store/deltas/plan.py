"""Compositional query plan executor.

Accepts a JSON query plan with named steps. Each step is one of:
  search, filter, intersect, union, diff, bridge, aggregate, chain.

Execution uses a hybrid approach: consecutive SQL-only steps compile into
a single CTE chain. Steps requiring Python (embedding text, computing
centroids from prior results) break the chain. The executor flushes the
CTE batch, does the Python work, then starts a new batch.
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
from deltas.store import _format_ts, _vec_to_list


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
                    "search, filter, intersect, union, diff, bridge, aggregate, chain"
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
        ):
            if getattr(step, action) is not None:
                return action
        return None

    def _get_refs(self, step: PlanStep, action: str) -> list[str]:
        if action in ("intersect", "union", "diff", "bridge"):
            return getattr(step, action, []) or []
        if action in ("aggregate", "chain"):
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
        limit = min(step.limit, 2000)
        params.append(limit)

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
        return [self._row_to_dict(r) for r in rows]

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

        limit = min(step.limit, 1000)

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

        rows = await self._pool.fetch(sql, centroid_a, centroid_b, exclude_ids, limit)
        results = []
        for r in rows:
            d = self._row_to_dict(r)
            d["distance"] = float(r["bridge_dist"])
            results.append(d)
        return results

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
        )


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO timestamp string to a timezone-aware datetime."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts).replace(tzinfo=UTC)
