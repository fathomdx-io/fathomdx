---
title: How to troubleshoot an install
description: Common reasons a fresh Fathom install fails to come up, organized by symptom, with the specific command to fix each one.
audience: operator
quadrant: how-to
last_verified: 2026-04-24
owners: [addons/scripts/install.sh, addons/scripts/preflight.sh, docker-compose.yml]
---

# How to troubleshoot an install

If `curl -fsSL https://fathomdx.io/install.sh | bash` ran but Fathom isn't reachable at `http://localhost:8201`, this page walks the diagnosis. Each section is one symptom and one fix.

## First, run preflight again

Almost every first-run failure is something preflight can detect or repair. From the install directory:

```bash
./addons/scripts/preflight.sh
```

Preflight is idempotent. Re-running it never makes things worse. It will tell you what it found and either fix it automatically or tell you what to fix yourself. If preflight prints nothing but green checks and the stack still won't come up, the rest of this page applies.

## Symptom: `connection refused` on port 8201

**Most likely cause:** the API service is still starting up. It waits for Postgres and the delta-store to be healthy before binding. Cold start can take 20-30 seconds.

```bash
docker compose ps
docker compose logs api --tail 50
```

If `api` is `Up` but not yet listening, give it another 10 seconds. If it's stuck in `Restarting`, the logs will name the reason. The most common is Postgres isn't ready yet, which clears itself within a minute or two.

## Symptom: container says `LAKE_DIR is blank` or `LAKE_DIR not set`

**Cause:** preflight wasn't run, or it was run but the `.env` file is missing required values.

```bash
./addons/scripts/preflight.sh
```

This prompts for `LAKE_DIR` if it's missing. Default is `~/.fathom/mind`. The directory and its required subdirectories will be created if they don't exist.

This is the bug that bit the first wave of installers in April 2026. Preflight now catches it; the install one-liner runs preflight before offering to start the stack. If you're running an older clone, `git pull` and re-run preflight.

## Symptom: `no such file or directory` on a bind-mount path

**Cause:** rootless Podman doesn't auto-create bind-mount targets the way Docker does. If `~/.fathom/mind/` and its subdirectories don't exist, the container fails to start.

```bash
./addons/scripts/preflight.sh
```

Preflight creates every required path. After it finishes, retry `docker compose up -d`.

## Symptom: `permission denied` on bind mounts (SELinux)

**Cause:** SELinux blocks containers from reading bind-mounted files unless the files are labeled correctly.

Two options. Pick one.

**Option 1: relabel the lake directory.**

```bash
chcon -Rt container_file_t ~/.fathom/
```

**Option 2: append `:z` to every volume mount in `docker-compose.yml`.**

```yaml
volumes:
  - ~/.fathom/mind:/data:z
```

The `:z` tells Podman to relabel as a shared volume. Either fix is permanent for that install.

## Symptom: `401 Unauthorized` from the dashboard

**Cause:** the `api` container and the `delta-store` container are configured with different `DELTA_API_KEY` values, so the API can't talk to its own lake.

Open `.env` and check `DELTA_API_KEY`. Either set it to a single value used by both services, or remove the line entirely (a blank value means "no auth between internal services," which is fine when the stack is bound to 127.0.0.1).

After editing `.env`:

```bash
docker compose up -d
```

Compose restarts only the affected containers.

## Symptom: chat replies fail with quota or auth errors

**Cause:** the LLM provider is reachable but rejecting requests.

```bash
docker compose logs api --tail 100 | grep -i 'rate\|quota\|401\|403'
```

For Gemini's free tier, the daily quota is generous but not infinite. If you've blown through it, [change LLM provider](./change-llm-provider.md) to OpenAI or a local Ollama install.

For OpenAI, a 401 means the key in `.env` is wrong or expired. Generate a new one and update `LLM_API_KEY`.

## Symptom: install one-liner failed mid-clone

**Cause:** network or git issue during the initial clone into `~/.fathom/src`.

```bash
rm -rf ~/.fathom/src
curl -fsSL https://fathomdx.io/install.sh | bash
```

The script is safe to re-run from scratch. Your lake state at `~/.fathom/mind/` is unaffected.

If the second run also fails, clone manually to inspect:

```bash
git clone https://github.com/fathomdx-io/fathomdx.git ~/.fathom/src
cd ~/.fathom/src
./addons/scripts/preflight.sh
docker compose up -d
```

## Symptom: dashboard loads but the lake looks empty

**Cause:** services are up but `delta-store` isn't reachable from `api`, or your initial profile write got rejected.

```bash
curl http://localhost:4246/health
# {"status":"ok"}

curl http://localhost:8201/v1/stats
# should show delta counts (start at 0 on a fresh install, increase as you use it)
```

If `/v1/stats` errors, the API can't reach the delta-store. Check `docker compose logs delta-store` for boot errors. Most often the Postgres volume is corrupt (Dropbox sync, host crash mid-write); if so, see [back up and restore the lake](./back-up-and-restore-the-lake.md) for recovery.

## When none of the above fits

1. Capture full logs:
   ```bash
   docker compose logs > fathom-install.log
   ```
2. Note your platform: OS, Docker or Podman, rootless or rootful, host architecture.
3. Open an issue at https://github.com/fathomdx-io/fathomdx/issues with the log attached and the symptoms you saw.

If you suspect the install one-liner itself has a bug, the install-smoke CI workflow (`.github/workflows/install-smoke.yml`) runs the install end-to-end on every PR. A failing run there is a confirmed regression worth flagging.
