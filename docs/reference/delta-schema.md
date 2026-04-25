---
title: Delta schema
description: The wire shape of a delta. Every field, its type, and what it means. Reference for writers (Write, batch) and readers (OpenAPI responses, recall).
audience: developer
quadrant: reference
last_verified: 2026-04-24
owners: [delta-store/deltas/models.py, delta-store/deltas/store.py]
---

# Delta schema

A delta is Fathom's atomic memory entry. Every write, every recall, every source ingest produces one. This page documents the wire shape as it appears in the HTTP API (`POST /v1/deltas`, `GET /v1/deltas`) and in the underlying delta-store.

Two shapes exist. The input shape (`DeltaIn`) is what you send when writing. The output shape (`DeltaOut`) is what you get back when reading. They differ in what's required versus derived.

## DeltaIn (write shape)

What a client sends on `POST /v1/deltas`:

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `content` | `string` | yes | — | The text of the observation. Free-form. One idea per delta. |
| `modality` | `string` | no | `"text"` | Currently only `"text"` and image-bearing deltas. |
| `tags` | `list[string]` | no | `[]` | Free vocabulary. Conventions in [reserved-tags-spec.md](./reserved-tags-spec.md). |
| `timestamp` | `string (ISO 8601)` | no | server time | When the observation happened. Defaults to now if omitted. |
| `id` | `string` | no | generated | Stable id. If omitted, the server generates one. Provide your own only for idempotent writes. |
| `embedding` | `list[float]` | no | computed | Vector representation. Usually omitted; the server computes it from `content`. |
| `provenance_embedding` | `list[float]` | no | computed | Vector over source + tags. Used for provenance-weighted recall. Usually omitted. |
| `source` | `string` | no | `"unknown"` | Who wrote this delta. Common values: `claude-code`, `fathom-agent`, `rss`, `user`, `source-runner`. |
| `media_hash` | `string` | no | — | Reference to an image moment. Set when the delta is image-bearing. |
| `expires_at` | `string (ISO 8601)` | no | — | TTL. Deltas past this timestamp are reaped. See [delete a delta or tag](../how-to/delete-a-delta-or-tag.md). |

Minimal write:

```json
{
  "content": "Jeremy installed Fathom on a lunch break.",
  "tags": ["contact:jeremy", "milestone"],
  "source": "manual"
}
```

The server fills in `id`, `timestamp`, `embedding`, `provenance_embedding`, and `modality`.

## DeltaOut (read shape)

What the API returns on reads:

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | Always present. Stable. |
| `timestamp` | `string (ISO 8601)` | Always present. Derived from the input's `timestamp` or server time at write. |
| `modality` | `string` | `"text"` or `"image"`. |
| `content` | `string` | The observation text. |
| `embedding` | `list[float]` | The computed semantic vector. Omitted from `DeltaSlim` responses. |
| `provenance_embedding` | `list[float]` | The provenance vector. Omitted from `DeltaSlim` responses. |
| `source` | `string` | Canonical source name. |
| `tags` | `list[string]` | Canonical tag list. |
| `media_hash` | `string \| null` | Present for image moments. |
| `expires_at` | `string \| null` | Present if a TTL was set. |

## DeltaSlim (read shape, no embeddings)

Identical to `DeltaOut` except `embedding` and `provenance_embedding` are omitted. Use this shape when recalling for display; the vectors are expensive to ship and the UI doesn't need them.

Most higher-level endpoints (`/v1/deltas`, `/v1/tools/remember`, `/v1/tools/recall`) return `DeltaSlim` by default. `POST /v1/deltas/batch-get` with explicit IDs returns full `DeltaOut`.

## Searching

Recall endpoints return a `ScoredDelta`:

```json
{
  "delta": { DeltaSlim },
  "score": 0.87,
  "semantic_distance": 0.14,
  "provenance_distance": 0.08,
  "recency_weight": 0.92
}
```

`score` is the composite rank the search ordered by. The per-dimension distances and weights are shown for transparency; client code rarely needs them.

## Tag conventions (short reference)

For the full catalogue see [reserved-tags-spec.md](./reserved-tags-spec.md). A few conventions that show up constantly:

| Pattern | Meaning |
|---|---|
| `chat:<slug>` | Belongs to that chat session. |
| `contact:<slug>` | Written by or about that contact. |
| `source:<name>` | Canonical source marker (duplicates the `source` field as a tag for filterability). |
| `routine-id:<id>` | Part of a routine's lifecycle (spec, fire, or summary). |
| `participant:user` / `participant:fathom` | Who spoke in a chat turn. |
| `deleted` | Tombstone; recall filters it by default. |
| `crystal:<kind>` | A compacted identity/feed/mood/drift artifact. |

## The storage shape

For reference (readers don't normally see this), the Postgres row has the same fields plus a few internals: `created_at` (server-set, separate from `timestamp`), `updated_at` (effectively immutable; only `expires_at` edits would bump it), and a few indexed helpers. The API never exposes the internal columns; clients only see what's in `DeltaOut` / `DeltaSlim`.

## Invariants

Three rules the schema enforces:

1. **`content` is required and non-empty.** A delta with no content has nothing to distill; the server rejects it.
2. **`tags` are strings, max 64 chars each.** Longer tags are trimmed; malformed entries are dropped.
3. **`id` collisions are rejected.** If you pass an `id` that already exists, the write fails. Use this intentionally for idempotent writers (a hook that might fire twice on the same event); don't use it for regular writes.

## Wire examples

**Write a chat turn:**

```json
POST /v1/deltas
{
  "content": "what did we decide about drift thresholds?",
  "tags": ["chat", "chat:blue-heron-plant", "participant:user"],
  "source": "claude-code"
}
```

**Write an image moment:**

```json
POST /v1/deltas/media
Content-Type: multipart/form-data

image=@screenshot.png
content="the dashboard after the drift spike cleared"
tags=["dashboard","screenshot","feed:drift"]
```

The server extracts the image into `${LAKE_DIR}/images/`, computes its hash, and writes the delta with `media_hash` set and `modality="image"`.

**Recall by tag:**

```json
GET /v1/deltas?tags_include=contact:jeremy&limit=20
```

Returns up to 20 `DeltaSlim` entries tagged `contact:jeremy`, most recent first.

**Semantic search:**

```json
POST /v1/tools/remember
{
  "query": "what did Jeremy think of Fathom?",
  "depth": "shallow",
  "limit": 10
}
```

Returns a `SearchResult` whose `results` are `ScoredDelta` entries.

## Things to know

- **The schema is stable.** Backward-compatible additions happen; renames and removals would bump a version. The current shape has been stable since v1.
- **The `modality` field is aspirational for now.** Only `text` and `image` exist; audio and other modalities are a future direction.
- **`id` and `timestamp` are separate.** `id` is stable; `timestamp` reflects when the observation happened (which may be different from when the delta was written). For most sources they're close; for feed ingests the `timestamp` matches the item's published date.
- **Embeddings live in the same row.** Fathom doesn't have a separate vector database; Postgres plus pgvector holds everything. See [compose-services.md](./compose-services.md).
- **`provenance_embedding` vs `embedding`.** The first is over source + tags; the second is over content. Having both lets recall weight by semantic similarity and provenance independently.
