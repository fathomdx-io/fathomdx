---
title: How to delete a delta or scope a tag away
description: Three ways to remove things from the lake. Soft delete via tombstone tag (recommended), TTL via expires_at, and hard delete via direct SQL (use carefully).
audience: operator
quadrant: how-to
last_verified: 2026-04-24
owners: [delta-store/deltas/store.py, delta-store/deltas/server.py]
---

# How to delete a delta or scope a tag away

The lake is append-only by design. "Delete" is not the system's first instinct, because reliable memory depends on past entries staying put. That said, three paths exist for removing or hiding things, ordered by how aggressive they are.

## Path 1: soft delete via tombstone tag

This is what you want most of the time. Mark a delta as deleted by writing a *new* delta that tombstones it. The old delta still exists in the lake, but every recall that respects the tombstone convention will skip it.

The tombstone delta:

- References the original delta's id.
- Carries the tag `deleted` (or another agreed convention).
- Has empty content, or a brief note explaining why.

In practice, fathomdx's higher-level operations (sessions, sources, routines, contacts) all do this when you "delete" them via the dashboard. The dashboard never hard-deletes; it tombstones. Deleted sessions, deleted routines, deleted feeds: all still in the lake, all filtered out of the UI.

For a one-off delta you want hidden, write a tombstone:

```bash
curl -X POST 'http://localhost:8201/v1/deltas' \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "content": "(tombstone)",
    "tags": ["deleted", "tombstone-for:<original-delta-id>"],
    "source": "manual"
  }'
```

Recall queries can then exclude this:

```bash
curl 'http://localhost:8201/v1/deltas?tags_exclude=deleted&limit=10'
```

## Path 2: scope a tag away

To stop a *category* of things from showing up in recall without altering individual deltas, filter by tag at the recall layer. Most fathomdx queries accept `tags_exclude=...` to drop specific tags from results.

Example: hide everything from a specific feed, without deleting it.

```bash
curl 'http://localhost:8201/v1/deltas?tags_exclude=feed:https://example.com/rss.xml&limit=20'
```

This is reversible by simply not passing `tags_exclude` next time. The deltas are intact.

For a more permanent "I don't want this in my recall ever" stance, write tombstone deltas for each of them, or use TTL.

## Path 3: TTL via expires_at

When you write a delta, you can give it an `expires_at` timestamp. The reaper job runs periodically and hard-deletes any delta whose expiration is in the past.

```bash
curl -X POST 'http://localhost:8201/v1/deltas' \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "content": "Reminder to ping Jeremy about Fathom on 2026-05-01",
    "tags": ["reminder"],
    "source": "manual",
    "expires_at": "2026-05-02T00:00:00Z"
  }'
```

After the TTL passes, the delta is gone from Postgres. The reaper logs each batch it deletes.

This is the right path for things you know in advance shouldn't live forever: short-lived UI state, transient tool-use events, time-bound reminders. Most plugins that write ephemeral deltas already set `expires_at` themselves.

For a delta that's already in the lake without a TTL, you can't add one after the fact via the public API. You can either tombstone it (path 1) or hard-delete (path 4).

## Path 4: hard delete via SQL (last resort)

Genuinely removing a delta from the lake (for a leaked secret, a true privacy concern, or a piece of data that legally must not exist) requires direct database access. There's no public HTTP endpoint for hard delete, by design. The friction is intentional.

```bash
# 1. Find the delta id you want gone.
curl 'http://localhost:8201/v1/deltas?limit=20' | jq '.[] | {id, content}'

# 2. Connect to Postgres and delete by id.
docker compose exec postgres psql -U postgres -d fathom -c \
  "DELETE FROM deltas WHERE id = 'the-delta-id-here';"
```

This is irreversible. The delta is gone from Postgres. If it had an associated image, the image file in `LAKE_DIR/images/` may still be there; remove it manually if needed.

Use this when the data legitimately should not exist and the soft paths aren't enough. For everything else, prefer path 1 or path 2.

## Things to know

- **Tombstones don't break recall, they shape it.** A tombstoned delta still has an embedding and still shows up in raw vector search. The filter happens at the query layer when `tags_exclude=deleted` is passed (which the dashboard does by default).
- **Search the tombstone.** "What did I delete and when?" is a real query. `tags_include=deleted` returns just the tombstones. The history of removals is itself memory.
- **Don't hard-delete to fix UI clutter.** That's what soft delete is for. Hard delete is for things that legally or ethically must not exist.
- **Routine, source, and session "delete" buttons in the dashboard are tombstones.** They write a new spec/source/session delta with `deleted: true`. The history persists. To genuinely remove, follow path 4 against each delta.
- **Image moments are two-part.** The delta is in Postgres; the image file is under `LAKE_DIR/images/`. Hard-deleting the delta leaves the file orphaned. There's no automatic cleanup; sweep periodically with a script if image storage matters.
- **Backup before hard delete.** Path 4 is one of the few operations that can corrupt your view of history. [Take a backup](./back-up-and-restore-the-lake.md) first.
