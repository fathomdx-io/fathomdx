---
title: How to update Fathom
description: Pull the latest fathomdx, rebuild the stack, restart. Three commands. No data loss.
audience: operator
quadrant: how-to
last_verified: 2026-04-24
owners: [docker-compose.yml, addons/scripts/install.sh]
---

# How to update Fathom

Updates pull new code, rebuild the local images, and restart the containers. The lake is not touched. Your Postgres volume, your `LAKE_DIR` state, your tokens, your routines: all preserved.

## Prerequisites

- A running Fathom stack installed via the curl one-liner or git clone.
- About a minute of downtime while the new images build and containers cycle.

## The three commands

```bash
cd ~/.fathom/src   # or wherever your install lives
git pull
docker compose build
docker compose up -d
```

That's it. Compose detects which containers' images changed and recreates only those. Containers whose images didn't change keep running.

If you cloned somewhere other than `~/.fathom/src`, replace the `cd` with that path. Set `FATHOM_DIR=...` during install to control this.

## Verify

After the up:

```bash
curl http://localhost:8201/v1/stats
# {"total": ..., "embedded": ..., ...}

curl http://localhost:4246/health
# {"status":"ok"}
```

Open the dashboard at `http://localhost:8201` and check that your existing sessions and routines are still there. They should be.

## When things change

Most updates are transparent. Occasionally an update touches one of these and needs your attention:

- **`.env.example` changed.** New env vars or renames. Run `diff .env .env.example` after pulling. Add anything new to your `.env`. Don't blindly copy over the example; you'll lose your own values.
- **Database schema changed.** Migrations run automatically on api boot. Watch `docker compose logs api` during the first up; you'll see the migration steps. If a migration fails, the api won't start, and the logs name the row count it stopped on. File an issue with the log; don't try to skip migrations by hand.
- **A breaking change is announced.** Check the release notes (or the most recent commits in `git log --oneline -20`). Backward-incompatible changes are flagged in the commit message. The README and CHANGELOG (when one exists) call out anything that requires manual intervention.

## Roll back if something broke

```bash
git log --oneline -10                    # find the SHA of the last working version
git checkout <previous-sha>
docker compose build
docker compose up -d
```

This is safe. The lake doesn't care which version of the code is talking to it; the schema is forward-compatible across patches, and major schema bumps are announced.

If a forward-only schema migration ran during the update and you need to roll back across it, restore from a [backup](./back-up-and-restore-the-lake.md) taken before the update.

## Update install.sh while I'm at it

The install one-liner served at `https://fathomdx.io/install.sh` is built from `addons/scripts/install.sh` in the repo. Updates to the installer don't change running installs (you have your own copy). If you want to rerun the installer for a fresh look:

```bash
curl -fsSL https://fathomdx.io/install.sh | bash
```

The script is idempotent. It detects an existing install, offers to refresh it instead of clobbering, and runs preflight either way.

## Things to know

- **`docker compose down -v` is not part of an update.** That command drops the Postgres volume. An update is `down + build + up`, never `down -v`.
- **Don't run two updates back-to-back too fast.** Wait for the api to fully come up after the first one before running the second. Otherwise the second build can race with the api's startup migration.
- **Your routines keep firing through an update.** The lake scheduler isn't tied to the api; spec deltas are read fresh on the next 30-second poll after the new api boots. A routine that was supposed to fire during your update window might fire a few seconds late. It won't get skipped.
- **Agent versions are independent.** `npx fathom-agent run` always pulls the current published agent. To update agents on paired hosts, restart them; npx will fetch the latest.
- **First boot after a fresh-pull rebuild is slower than usual.** The api waits for postgres, runs migrations, and re-indexes some derived tables. Give it 60-90 seconds before assuming something's wrong.
