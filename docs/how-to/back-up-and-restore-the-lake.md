---
title: How to back up and restore the lake
description: Two-part backup (Postgres dump + LAKE_DIR archive), and how to restore either piece on a new machine. The lake survives anything that doesn't take both copies with it.
audience: operator
quadrant: how-to
last_verified: 2026-04-24
owners: [docker-compose.yml, .env.example]
---

# How to back up and restore the lake

The lake lives in two places: Postgres (deltas, embeddings, sessions, contacts) and `LAKE_DIR` on disk (images, drift history, mood state, API tokens, backups). A complete backup captures both. Either one alone is incomplete.

## Prerequisites

- A running Fathom stack (or a stopped one with the postgres volume intact).
- Read access to `LAKE_DIR` on the host. By default this is `~/.fathom/mind/`.
- Write access to wherever you want the backup to land.

## Take a backup

The two copies don't have to happen at the same instant, but it's cleaner if they do. Stop the stack first if you want a perfectly consistent snapshot; otherwise an active stack produces a "live" backup that's almost always fine for personal use.

```bash
# 1. Postgres dump.
docker compose exec -T postgres pg_dump -U postgres -Fc fathom > fathom-pg-$(date +%Y%m%d).dump

# 2. LAKE_DIR archive.
tar czf fathom-lake-$(date +%Y%m%d).tar.gz -C ~/.fathom mind
```

The Postgres dump uses the custom format (`-Fc`), which is compressed, restorable to any version of Postgres, and faster than plain SQL. The tarball captures everything under `LAKE_DIR` including images and tokens.

For a fully cold backup, stop first:

```bash
docker compose down
# then run the two commands above
docker compose up -d
```

You'll lose maybe 30 seconds of polling during the down. If the lake is mid-write to a routine summary or a feed item, those go to the next poll.

## Restore on the same machine

Reverse:

```bash
# 1. Stop the stack so nothing's writing.
docker compose down

# 2. Drop and recreate the database.
docker compose up -d postgres
docker compose exec -T postgres psql -U postgres -c 'DROP DATABASE IF EXISTS fathom;'
docker compose exec -T postgres psql -U postgres -c 'CREATE DATABASE fathom;'

# 3. Restore the dump.
docker compose exec -T postgres pg_restore -U postgres -d fathom < fathom-pg-YYYYMMDD.dump

# 4. Restore LAKE_DIR.
rm -rf ~/.fathom/mind
tar xzf fathom-lake-YYYYMMDD.tar.gz -C ~/.fathom

# 5. Bring the rest of the stack back up.
docker compose up -d
```

The dashboard at `http://localhost:8201` should come up with the lake intact: same chat sessions, same deltas, same identity crystal.

## Restore on a different machine

Same steps as above, with one extra: `LAKE_DIR` in `.env` must match where you actually put the directory. If your old install used `LAKE_DIR=/home/old-user/.fathom/mind` and you're restoring to `/home/new-user/.fathom/mind`, edit `.env` to reflect the new path before the final `docker compose up -d`.

Run `./addons/scripts/preflight.sh` after the edit to confirm the path resolves and the required subdirectories exist.

## Just the deltas, no Postgres

If you only need a portable record of the lake's contents (for archival, offline analysis, or import into another tool), use the export endpoint:

```bash
curl 'http://localhost:8201/v1/deltas?limit=10000' > fathom-deltas-$(date +%Y%m%d).json
```

Pagination is via `before=<timestamp>`. For a complete export of a busy lake, page through until the response is empty.

This is read-only and doesn't include Postgres internals, embeddings, or sessions. Useful for "I want a copy of what Fathom remembers." Not useful for "I want to restore Fathom on another machine"; for that, you need both pieces.

## Backing up automatically

Set up a routine that runs the two commands above on a schedule. See [set up a routine](./set-up-a-routine.md) for the mechanism. A reasonable cadence is nightly:

```
0 2 * * *
```

Have the routine drop the dump and tarball into a directory you sync to S3, Backblaze, or wherever you keep important things. Keep at least three rolling copies; daily backups that get overwritten the next day don't help when you discover the corruption two weeks later.

## Things to know

- **Don't bind-mount the Postgres data directory through Dropbox or any other syncing filesystem.** Postgres writes pages non-atomically; sync layers corrupt them mid-write. The named Docker volume (`${COMPOSE_PROJECT_NAME}-pg`) is on local disk by design.
- **Image moments are stored under `LAKE_DIR`, not in Postgres.** The Postgres dump alone has the references but not the images. Restoring without `LAKE_DIR` leaves you with broken image links.
- **API tokens are in `LAKE_DIR/tokens/`.** Restoring `LAKE_DIR` brings those back, including any agent and integration tokens. If you'd rather rotate everything on restore, delete the tokens directory before bringing the stack up.
- **The identity crystal is stored as a delta in Postgres, not separately.** It comes back with the dump.
- **`docker compose down -v` destroys the postgres volume.** This is the one command that erases your lake locally. It's deliberately separate from `docker compose down` (which preserves it). Treat it the way you'd treat `rm -rf` of any other state directory.
