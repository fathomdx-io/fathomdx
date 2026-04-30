"""Bounding box query engine — Postgres + pgvector edition.

Semantic candidate retrieval uses pgvector's HNSW index via <=> operator.
Temporal and provenance filtering still happen in Python to preserve exact
v1 bounding-box semantics. Sessions and subsets remain in-memory.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncpg
import numpy as np

from deltas.embedder import embed_image, embed_text
from deltas.models import (
    DeltaOut,
    DeltaSlim,
    DimensionWeights,
    ScoredDelta,
    ScoredDeltaSlim,
    SearchResult,
)
from deltas.store import DeltaStore, _format_ts, _vec_to_list

# ── Config ───────────────────────────────────────────────────────────────────


@dataclass
class QueryConfig:
    session_ttl: float = float(os.environ.get("QUERY_SESSION_TTL", "300"))
    max_sessions: int = int(os.environ.get("QUERY_MAX_SESSIONS", "1000"))


# ── Session store ────────────────────────────────────────────────────────────


@dataclass
class QuerySession:
    result_ids: set[str]
    last_used: float


class SessionStore:
    """In-memory cache of query session result sets with TTL."""

    def __init__(self, config: QueryConfig):
        self._sessions: dict[str, QuerySession] = {}
        self._config = config

    def get(self, session_id: str) -> QuerySession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.monotonic() - session.last_used > self._config.session_ttl:
            del self._sessions[session_id]
            return None
        session.last_used = time.monotonic()
        return session

    def put(self, session_id: str, result_ids: set[str]) -> None:
        self._evict_expired()
        if len(self._sessions) >= self._config.max_sessions:
            self._evict_oldest()
        self._sessions[session_id] = QuerySession(
            result_ids=result_ids,
            last_used=time.monotonic(),
        )

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, s in self._sessions.items() if now - s.last_used > self._config.session_ttl
        ]
        for sid in expired:
            del self._sessions[sid]

    def _evict_oldest(self) -> None:
        if not self._sessions:
            return
        oldest = min(self._sessions, key=lambda k: self._sessions[k].last_used)
        del self._sessions[oldest]

    @property
    def active_count(self) -> int:
        self._evict_expired()
        return len(self._sessions)


# ── Subset store ────────────────────────────────────────────────────────────


@dataclass
class Subset:
    delta_ids: set[str]
    queries: list[str]
    created: float
    last_used: float


class SubsetStore:
    """Ephemeral named sets of delta IDs. 1-hour TTL, garbage collected on access."""

    TTL = 3600  # 1 hour

    def __init__(self):
        self._subsets: dict[str, Subset] = {}

    def create(self, delta_ids: set[str], query: str) -> str:
        self._gc()
        subset_id = f"ss_{uuid.uuid4().hex[:8]}"
        now = time.monotonic()
        self._subsets[subset_id] = Subset(
            delta_ids=delta_ids,
            queries=[query],
            created=now,
            last_used=now,
        )
        return subset_id

    def get(self, subset_id: str) -> Subset | None:
        self._gc()
        subset = self._subsets.get(subset_id)
        if subset is None:
            return None
        subset.last_used = time.monotonic()
        return subset

    def broaden(self, subset_id: str, delta_ids: set[str], query: str) -> int:
        subset = self.get(subset_id)
        if subset is None:
            return -1
        subset.delta_ids |= delta_ids
        subset.queries.append(query)
        return len(subset.delta_ids)

    def _gc(self) -> None:
        now = time.monotonic()
        expired = [sid for sid, s in self._subsets.items() if now - s.last_used > self.TTL]
        for sid in expired:
            del self._subsets[sid]


# ── Distance computation ────────────────────────────────────────────────────


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cosine similarity. Returns 0 (identical) to 2 (opposite)."""
    if not a or not b:
        return 1.0
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    dot = np.dot(va, vb)
    norm = np.linalg.norm(va) * np.linalg.norm(vb)
    if norm == 0:
        return 1.0
    return float(1.0 - dot / norm)


def _temporal_distance(origin_ts: str, delta_ts: str, max_span_ms: float) -> float:
    """Normalized temporal distance (0-1)."""

    def _parse(ts: str) -> float:
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts).replace(tzinfo=UTC).timestamp() * 1000

    if max_span_ms == 0:
        return 0.0
    diff = abs(_parse(origin_ts) - _parse(delta_ts))
    return min(diff / max_span_ms, 1.0)


# ── Query engine ─────────────────────────────────────────────────────────────


def _new_session_id() -> str:
    return f"qs_{uuid.uuid4().hex[:8]}"


def _now_as_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _to_slim(s: ScoredDelta) -> ScoredDeltaSlim:
    """Strip embedding vectors from a scored delta."""
    d = s.delta
    return ScoredDeltaSlim(
        delta=DeltaSlim(
            id=d.id,
            timestamp=d.timestamp,
            modality=d.modality,
            content=d.content,
            source=d.source,
            tags=d.tags,
            media_hash=d.media_hash,
            expires_at=d.expires_at,
        ),
        distance=s.distance,
        dimensions=s.dimensions,
        engagement_cloud=s.engagement_cloud,
    )


# ── Engagement cloud ─────────────────────────────────────────────────────────
#
# A delta's engagement cloud is every other delta that points at it via an
# engagement pointer-tag: `engages:<id>`, `refutes:<id>`, `affirms:<id>`,
# `reply-to:<id>`, `from:<id>`. Provenance citations (`from:`) count as
# implicit positive engagement — a sediment citing a source is the mind
# building on that source, which is load-bearing weight.

ENGAGEMENT_POINTER_PREFIXES = ("engages", "refutes", "affirms", "reply-to", "from")
ENGAGEMENT_FETCH_CAP = 500  # max engagement deltas pulled per search
ENGAGEMENT_TOPK_CAP = 100  # only look up clouds for the top-N scored deltas
VALENCE_MAX_PCT = 0.30  # cap ±30% rank shift from valence


# ── Noise suppression ────────────────────────────────────────────────────────
#
# Search-time rerank that pushes generic, low-information deltas down without
# dropping them — short user interjections ("hey", "ok", "yeah"), throwaway
# corrections ("that's not what I wanted"), bare acks. These match the user's
# query in embedding space because they're vague enough to be near anything,
# not because they're load-bearing answers. We don't tag at write time
# (intent is hard to call from a single delta in isolation); we just penalize
# in ranking. Two compounding signals:
#   1. length: content under LENGTH_THRESHOLD chars takes a fixed bump
#   2. centroid: similarity to a seeded "generic noise" centroid → bump scaled
#      to NOISE_ALPHA when above NOISE_FLOOR
# Applied multiplicatively to distance (lower is better, so penalty > 1).

NOISE_SEEDS = (
    "hey",
    "ok",
    "okay",
    "yeah",
    "yep",
    "yes",
    "no",
    "nope",
    "sure",
    "wait",
    "stop",
    "huh",
    "hmm",
    "uh",
    "um",
    "lol",
    "haha",
    "what",
    "what's up",
    "hi",
    "hello",
    "hello!",
    "howdy",
    "hola",
    "thanks",
    "ty",
    "nvm",
    "nevermind",
    "actually",
    "test",
    "testing",
    "please",
    "gold",
    "confirmed",
    "approved",
    "got it",
    "agreed",
    "exactly",
    "perfect",
    "great",
    "cool",
    "done",
    "noted",
    "go ahead",
    "sure go",
    "sure, go",
    "do it",
    "do it again",
    "lgtm",
    "looks good",
    "sounds good",
    "ship it",
    "merge it",
    "that's not what i wanted",
    "no not that",
)
NOISE_ALPHA = 0.35  # max additional fraction added to distance from centroid term
NOISE_FLOOR = 0.55  # cosine similarity below this contributes nothing
LENGTH_THRESHOLD = 24  # chars; below → length penalty kicks in
LENGTH_PENALTY = 0.20  # multiplicative bump from length term

# Hard drop — a result that only matched because it's generic noise should
# not be in the candidate set at all. Softly down-ranking it isn't enough:
# when the lake has nothing better to offer for a query, "hello" still
# crowds onto the page. Hard drop runs alongside the soft modifier.
#
# We drop on length and exact-seed match only, NOT on centroid similarity:
# the seed centroid sits in the "short generic text" region of embedding
# space, and so does plenty of legitimate short content (dates, short
# questions, the query "alerts" itself sims at 0.87). A centroid threshold
# tight enough to catch "hello" is also tight enough to catch the load-
# bearing stuff. The exact-match check (NOISE_SEEDS includes the actual
# offenders) plus the length floor catches the right things without
# false-positives.
LENGTH_DROP_THRESHOLD = 10  # content shorter than this → drop
_NOISE_SEED_NORMALIZED = frozenset(s.strip().lower() for s in NOISE_SEEDS)

# Process-wide cache of the seed-centroid. The seeds are constants, so
# every QueryEngine and PlanExecutor in this process can share a single
# build — first caller pays the embed cost, all subsequent paths get a
# free reference. `None` = uncomputed; `[]` = computed-but-empty (embedder
# offline at build time, callers degrade gracefully).
_NOISE_CENTROID_CACHE: list[float] | None = None


def get_noise_centroid() -> list[float]:
    """Lazy-built normalized centroid of NOISE_SEEDS embeddings.

    Shared across the shallow `/search` path (QueryEngine) and the
    compositional `/plan` path (PlanExecutor) so both apply the same
    suppression. Returns `[]` if the embedder is offline — `_noise_modifier`
    treats that as a no-op for the centroid term.
    """
    global _NOISE_CENTROID_CACHE
    if _NOISE_CENTROID_CACHE is not None:
        return _NOISE_CENTROID_CACHE
    embeddings = []
    for seed in NOISE_SEEDS:
        try:
            e = embed_text(seed)
        except Exception:
            e = None
        if e:
            embeddings.append(e)
    if not embeddings:
        _NOISE_CENTROID_CACHE = []
        return _NOISE_CENTROID_CACHE
    arr = np.array(embeddings, dtype=np.float32)
    centroid = arr.mean(axis=0)
    norm = np.linalg.norm(centroid)
    if norm > 0:
        centroid = centroid / norm
    _NOISE_CENTROID_CACHE = centroid.tolist()
    return _NOISE_CENTROID_CACHE


def _noise_modifier(
    content: str | None,
    embedding: list[float],
    noise_centroid: list[float],
) -> float:
    """Multiplicative modifier on distance — >1 penalizes, =1 neutral.

    Both terms compound. Length is content-only (no embedding needed); the
    centroid term is skipped silently if either embedding is absent.
    """
    factor = 1.0
    text = (content or "").strip()
    if 0 < len(text) < LENGTH_THRESHOLD:
        factor *= 1.0 + LENGTH_PENALTY
    if noise_centroid and embedding:
        noise_sim = 1.0 - _cosine_distance(embedding, noise_centroid)
        excess = max(0.0, noise_sim - NOISE_FLOOR)
        scale = max(1.0 - NOISE_FLOOR, 0.001)
        factor *= 1.0 + NOISE_ALPHA * (excess / scale)
    return factor


def _is_pure_noise(
    content: str | None,
    embedding: list[float],
    noise_centroid: list[float],
) -> bool:
    """Hard-drop check — the row only matched because it's generic noise.

    Triggers on:
      • content shorter than LENGTH_DROP_THRESHOLD chars
      • content (stripped, lowercased) matches a noise seed exactly
    """
    del embedding, noise_centroid  # reserved for future centroid-based rules
    text = (content or "").strip()
    if not text:
        return True
    if len(text) < LENGTH_DROP_THRESHOLD:
        return True
    if text.lower() in _NOISE_SEED_NORMALIZED:
        return True
    return False


def _valence_modifier(cloud: list[dict]) -> float:
    """Multiplicative modifier on distance (lower distance = better rank).

    Each cloud member contributes by pointer-type and content:
      • `refutes:<id>` or JSON payload with `engagement:less` → -1
      • `affirms:<id>` or `engagement:more` → +1
      • `from:<id>` (provenance citation) → +0.5 (implicit, weaker)
      • `engages:<id>` or `reply-to:<id>` (neutral attention) → +0.25

    Net score is scaled so each point shifts rank by ~5%, clamped to
    ±VALENCE_MAX_PCT so no single cloud can nuke or anoint a delta.
    Silence returns 1.0 (no shift).
    """
    if not cloud:
        return 1.0
    score = 0.0
    for d in cloud:
        tags = d.get("tags") or []
        # Pointer-type signal
        for t in tags:
            if t.startswith("refutes:"):
                score -= 1.0
                break
            elif t.startswith("affirms:"):
                score += 1.0
                break
            elif t.startswith("from:"):
                score += 0.5
                break
            elif t.startswith("engages:") or t.startswith("reply-to:"):
                score += 0.25
                break
        # Feed-engagement valence signal (already-pointed deltas via engages:)
        if "engagement:less" in tags:
            score -= 0.5
        elif "engagement:more" in tags:
            score += 0.5
    shift = max(-VALENCE_MAX_PCT, min(VALENCE_MAX_PCT, score * 0.05))
    return 1.0 - shift


@dataclass
class QueryEngine:
    """Bounding box query with pgvector candidate retrieval."""

    store: DeltaStore
    pool: asyncpg.Pool
    config: QueryConfig = field(default_factory=QueryConfig)
    sessions: SessionStore = field(init=False)
    subsets: SubsetStore = field(init=False)

    def __post_init__(self):
        self.sessions = SessionStore(self.config)
        self.subsets = SubsetStore()

    async def _centroid_from_ids(self, ids: list[str]) -> list[float]:
        """Compute normalized centroid from stored embeddings of the given delta IDs."""
        embeddings = []
        for delta_id in ids:
            d = await self.store.get(delta_id)
            if d and d.get("embedding"):
                embeddings.append(d["embedding"])
        if not embeddings:
            return []
        arr = np.array(embeddings, dtype=np.float32)
        centroid = arr.mean(axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm
        return centroid.tolist()

    async def _fetch_candidates_pgvector(
        self,
        origin_embedding: list[float],
        *,
        tags_include: list[str] | None = None,
        tags_exclude: list[str] | None = None,
        modality: str | None = None,
        limit: int = 2000,
    ) -> list[dict]:
        """Use pgvector HNSW to fetch nearest candidates, with optional filters."""
        emb_arr = np.array(origin_embedding, dtype=np.float32)

        conditions = [
            "d.embedding IS NOT NULL",
            "(d.expires_at IS NULL OR d.expires_at > NOW())",
        ]
        params: list = [emb_arr]
        idx = 2

        if tags_include:
            conditions.append(f"d.tags @> ${idx}")
            params.append(tags_include)
            idx += 1
        if tags_exclude:
            conditions.append(f"NOT (d.tags && ${idx})")
            params.append(tags_exclude)
            idx += 1
        if modality:
            conditions.append(f"d.modality = ${idx}")
            params.append(modality)
            idx += 1

        where = " AND ".join(conditions)
        params.append(limit)

        sql = f"""
            SELECT d.*,
                   (d.embedding <=> $1) AS s_dist,
                   (d.provenance_embedding <=> $1) AS p_dist
            FROM deltas d
            WHERE {where}
            ORDER BY d.embedding <=> $1
            LIMIT ${idx}
        """

        rows = await self.pool.fetch(sql, *params)

        results = []
        for r in rows:
            d = {
                "id": r["id"],
                "timestamp": _format_ts(r["timestamp"]),
                "modality": r["modality"],
                "content": r["content"],
                "embedding": _vec_to_list(r["embedding"]),
                "provenance_embedding": _vec_to_list(r["provenance_embedding"]),
                "source": r["source"],
                "tags": list(r["tags"]) if r["tags"] else [],
                "s_dist": float(r["s_dist"]) if r["s_dist"] is not None else 1.0,
                "p_dist": float(r["p_dist"]) if r["p_dist"] is not None else 1.0,
            }
            if r["media_hash"]:
                d["media_hash"] = r["media_hash"]
            if r["expires_at"]:
                d["expires_at"] = _format_ts(r["expires_at"])
            results.append(d)

        return results

    async def _fetch_engagement_cloud(self, target_ids: list[str]) -> dict[str, list[dict]]:
        """Batched lookup: for each target id, return engagement deltas pointing at it.

        A single query fetches all deltas whose tags include any engagement
        pointer-tag aimed at any target. Results are then bucketed by target
        id based on which pointer-tag(s) matched.
        """
        if not target_ids:
            return {}
        pointers = [
            f"{prefix}:{tid}" for prefix in ENGAGEMENT_POINTER_PREFIXES for tid in target_ids
        ]
        sql = """
            SELECT d.id, d.timestamp, d.modality, d.content, d.source, d.tags,
                   d.media_hash, d.expires_at
            FROM deltas d
            WHERE d.tags && $1
              AND (d.expires_at IS NULL OR d.expires_at > NOW())
            ORDER BY d.timestamp DESC
            LIMIT $2
        """
        rows = await self.pool.fetch(sql, pointers, ENGAGEMENT_FETCH_CAP)

        targets = set(target_ids)
        cloud: dict[str, list[dict]] = {tid: [] for tid in target_ids}
        for r in rows:
            row_tags = list(r["tags"]) if r["tags"] else []
            d = {
                "id": r["id"],
                "timestamp": _format_ts(r["timestamp"]),
                "modality": r["modality"],
                "content": r["content"],
                "source": r["source"],
                "tags": row_tags,
                "media_hash": r["media_hash"] or None,
                "expires_at": _format_ts(r["expires_at"]) if r["expires_at"] else None,
            }
            for tag in row_tags:
                if ":" not in tag:
                    continue
                prefix, _, value = tag.partition(":")
                if prefix in ENGAGEMENT_POINTER_PREFIXES and value in targets:
                    cloud[value].append(d)
        return cloud

    async def search(
        self,
        origin: str | None = None,
        origin_ids: list[str] | None = None,
        origin_image: str | None = None,
        radius: float = 0.7,
        radii: DimensionWeights | None = None,
        session_id: str | None = None,
        tags_include: list[str] | None = None,
        tags_exclude: list[str] | None = None,
        modality: str | None = None,
        create_subset: bool = False,
        subset_id: str | None = None,
        limit: int = 50,
        weights: DimensionWeights | None = None,
        include_engagement_cloud: bool = False,
        suppress_noise: bool = True,
    ) -> SearchResult:
        r = radii or weights or DimensionWeights()
        r_t = r.temporal
        r_s = r.semantic
        r_p = r.provenance

        # 1. Compute origin embedding
        if origin_ids:
            origin_embedding = await self._centroid_from_ids(origin_ids)
            if not origin_embedding:
                origin_embedding = embed_text(origin) if origin else []
        elif origin_image:
            origin_embedding = embed_image(origin_image)
            if origin:
                text_emb = embed_text(origin)
                arr = np.array([origin_embedding, text_emb], dtype=np.float32).mean(axis=0)
                norm = np.linalg.norm(arr)
                origin_embedding = (arr / norm).tolist() if norm > 0 else arr.tolist()
        elif origin:
            origin_embedding = embed_text(origin)
        else:
            new_sid = session_id or _new_session_id()
            self.sessions.put(new_sid, set())
            return SearchResult(session_id=new_sid, full=True, results=[], added=[], removed=[])

        # 2. Fetch candidates via pgvector HNSW
        candidates = await self._fetch_candidates_pgvector(
            origin_embedding,
            tags_include=tags_include,
            tags_exclude=tags_exclude,
            modality=modality,
        )

        # 2b. Scope to subset if provided
        if subset_id:
            subset = self.subsets.get(subset_id)
            if subset is None:
                raise ValueError(
                    f"Subset {subset_id} not available — create a new one to get an updated ID"
                )
            allowed = subset.delta_ids
            candidates = [d for d in candidates if d["id"] in allowed]

        if not candidates:
            new_sid = session_id or _new_session_id()
            self.sessions.put(new_sid, set())
            return SearchResult(session_id=new_sid, full=True, results=[], added=[], removed=[])

        # 3. Compute temporal span for normalization
        timestamps_ms = []
        for d in candidates:
            try:
                ts = d["timestamp"].replace("Z", "+00:00")
                timestamps_ms.append(
                    datetime.fromisoformat(ts).replace(tzinfo=UTC).timestamp() * 1000
                )
            except Exception:
                timestamps_ms.append(0)
        max_span_ms = max(timestamps_ms) - min(timestamps_ms) if timestamps_ms else 1.0
        max_span_ms = max(max_span_ms, 1.0)

        origin_provenance = origin_embedding

        # 4. Score — per-dimension independent filtering
        scored: list[ScoredDelta] = []
        noise_centroid = get_noise_centroid() if suppress_noise else []
        for d in candidates:
            t_dist = _temporal_distance(_now_as_iso(), d["timestamp"], max_span_ms)
            s_dist = d.get("s_dist", _cosine_distance(origin_embedding, d["embedding"]))
            p_dist = _cosine_distance(origin_provenance, d.get("provenance_embedding", []))

            if t_dist > r_t or s_dist > r_s or p_dist > r_p:
                continue

            if suppress_noise and _is_pure_noise(
                d.get("content"), d.get("embedding") or [], noise_centroid
            ):
                continue

            max_r = max(r_t, r_s, r_p, 0.001)
            distance = max(t_dist / max_r, s_dist / max_r, p_dist / max_r)

            if suppress_noise:
                distance *= _noise_modifier(
                    d.get("content"), d.get("embedding") or [], noise_centroid
                )

            scored.append(
                ScoredDelta(
                    delta=DeltaOut(**{k: v for k, v in d.items() if k not in ("s_dist", "p_dist")}),
                    distance=round(distance, 4),
                    dimensions=DimensionWeights(
                        temporal=round(t_dist, 4),
                        semantic=round(s_dist, 4),
                        provenance=round(p_dist, 4),
                    ),
                )
            )

        scored.sort(key=lambda s: s.distance)
        total_relevant = len(scored)

        # 4b. Engagement cloud augmentation — attach cloud members and fold
        # valence into rank. Only on opt-in; shallow recall stays raw and fast.
        if include_engagement_cloud and scored:
            top_ids = [s.delta.id for s in scored[:ENGAGEMENT_TOPK_CAP]]
            cloud_by_id = await self._fetch_engagement_cloud(top_ids)
            for s in scored[:ENGAGEMENT_TOPK_CAP]:
                cloud_rows = cloud_by_id.get(s.delta.id, [])
                if not cloud_rows:
                    continue
                s.engagement_cloud = [DeltaSlim(**row) for row in cloud_rows]
                s.distance = round(s.distance * _valence_modifier(cloud_rows), 4)
            scored.sort(key=lambda s: s.distance)

        # 5. Create subset from results if requested
        result_subset_id = None
        result_subset_size = None
        if create_subset and scored:
            result_ids_for_subset = {s.delta.id for s in scored}
            query_label = origin or (origin_ids[0] if origin_ids else "unknown")
            result_subset_id = self.subsets.create(result_ids_for_subset, query_label)
            result_subset_size = len(result_ids_for_subset)

        # 6. Compute origin_radius
        origin_radius = None
        if origin_ids:
            origin_set = set(origin_ids)
            origin_dists = [s.distance for s in scored if s.delta.id in origin_set]
            if origin_dists:
                origin_radius = max(origin_dists) * 1.05

        # 7. Session diffing
        new_ids = {s.delta.id for s in scored}

        if session_id:
            existing = self.sessions.get(session_id)
            if existing is not None:
                added_ids = new_ids - existing.result_ids
                removed_ids = existing.result_ids - new_ids
                self.sessions.put(session_id, new_ids)

                added = [s for s in scored if s.delta.id in added_ids]
                added = added[:limit]
                return SearchResult(
                    session_id=session_id,
                    full=False,
                    results=[],
                    added=[_to_slim(s) for s in added],
                    removed=sorted(removed_ids),
                    total_relevant=total_relevant,
                    subset_id=result_subset_id,
                    subset_size=result_subset_size,
                )

        # 8. Apply limit + strip embeddings
        capped = scored[:limit]

        sid = session_id or _new_session_id()
        self.sessions.put(sid, new_ids)
        return SearchResult(
            session_id=sid,
            full=True,
            results=[_to_slim(s) for s in capped],
            added=[],
            removed=[],
            total_relevant=total_relevant,
            origin_radius=origin_radius,
            subset_id=result_subset_id,
            subset_size=result_subset_size,
        )
