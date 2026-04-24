---
title: How to set up a routine
description: Schedule a prompt to run on your machine on a cadence. Either chat with Fathom and let it draft one, or write the spec yourself in the dashboard's Routines page.
audience: developer
quadrant: how-to
last_verified: 2026-04-24
owners: [api/routes/routines.py, addons/agent/plugins/kitty.js, reference/routine-spec.md]
---

# How to set up a routine

A routine is a prompt + a cron schedule + a place to run. When the time comes, the kitty plugin on your paired agent spawns a window, runs Claude Code inside it, and injects the prompt. The output lands back in the lake as a `routine-summary` delta.

This page covers two paths: ask Fathom to draft a routine for you in chat, or build it yourself in the dashboard. Both produce the same artifact (a spec delta).

## Prerequisites

- A paired agent on the machine you want the routine to run on (see [tutorial 3](../tutorials/03-fathom-does-things.md) or [pair another machine](./pair-another-machine.md)).
- kitty installed and on the agent's `PATH`. `kitty --version` from the agent's shell should work.
- Claude Code installed and authenticated on the same machine.

If kitty is missing the routine will save fine but never fire. If Claude isn't authenticated the kitty window will open and immediately exit.

## Path A: ask Fathom to draft it

This is the easier flow when you know what you want but don't remember the cron syntax.

Open a Fathom chat session and describe what you want:

> Set me up with a routine that summarizes my GitHub notifications each weekday at 7am and posts a one-paragraph briefing.

Fathom drafts a routine: name, cron expression, workspace, and a prompt body. Review the draft. If something needs adjusting, say so. When you confirm, Fathom writes the spec delta to the lake.

The lake scheduler polls every 30 seconds and picks it up automatically. Open the **Routines** page in the dashboard to see it listed with its next-fire time.

## Path B: build it yourself in the dashboard

Open the dashboard's **Routines** page. Click **New routine**. Fill in:

| Field | What to put |
|---|---|
| **Name** | Human-readable label. Shown in the dashboard. |
| **Schedule** | A 5-field cron string. See cron examples below. |
| **Workspace** | Path to a directory on the agent host. Claude `cd`s here before running. Use `~/Dropbox/Work/your-project` or similar. |
| **Permission mode** | `auto` runs Claude with `--permission-mode auto` (classifier decides). `normal` prompts you in the kitty window for each tool use. |
| **Prompt** | What you want Claude to do when the routine fires. |

Save. The scheduler picks up the spec within 30 seconds.

### Cron expressions

| Schedule | Cron |
|---|---|
| Every weekday at 7am | `0 7 * * 1-5` |
| Every Saturday at 9am | `0 9 * * 6` |
| First of every month at 10am | `0 10 1 * *` |
| Every 4 hours | `0 */4 * * *` |
| Every hour on the hour | `0 * * * *` |

Cron is evaluated in the API container's local timezone (see `TZ` in `.env`). If you don't set one, it defaults to whatever the host has (often `America/Chicago` per the bundled compose config). Routines fire on container time, not your phone's time.

## Test the fire manually

Don't wait for the scheduled time to confirm the routine works. From the dashboard's Routines page, click the run-now action. This writes a `routine-fire` delta immediately. The kitty plugin sees it within a poll cycle and spawns the window.

What you should see:

1. A kitty window opens on the agent host.
2. Claude Code starts inside it with your prompt.
3. Claude does the work and writes a `routine-summary` delta.
4. The dashboard pairs the fire and summary by `fire-delta:<fire-id>` tag.

If step 1 doesn't happen, kitty isn't reachable from the agent's shell. If step 2 doesn't happen, `claude` isn't on the agent's `PATH` or isn't authenticated. If step 4 doesn't pair, the summary delta wasn't written; check `docker compose logs api` for errors.

## Edit a routine

Routines edit by writing a new spec delta with the same `routine-id:<id>` tag. The scheduler always uses the latest spec by timestamp. The dashboard's **Routines** page does this for you when you save changes.

You can also edit by hand. From the lake's HTTP API:

```bash
curl -X PUT 'http://localhost:8201/v1/routines/<routine-id>' \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{"schedule": "0 8 * * 1-5", "enabled": true, ...}'
```

The full field list is in [routine-spec.md](../reference/routine-spec.md).

## Pause a routine

Set `enabled: false` on the spec. The dashboard greys it out. The scheduler skips it. Re-enable later by setting `enabled: true`.

This is a real edit (a new spec delta), not a state flag. Audit history is preserved.

## Delete a routine

Set `deleted: true` on the spec. The dashboard hides it. The scheduler skips it.

The history of every fire and summary stays in the lake. Tombstoning means the routine stops running, not that it's erased.

## Common patterns

**Daily briefing.** Cron `0 7 * * 1-5`. Prompt: "Check overnight emails, GitHub notifications, and any new feed items tagged urgent. Summarize what needs my attention this morning."

**Cleanup task.** Cron `0 3 * * 0` (Sunday 3am). Prompt: "Walk the project at workspace, list any TODO comments older than 90 days, and open issues for them."

**Status report.** Cron `0 17 * * 5` (Friday 5pm). Prompt: "Summarize what got committed across my projects this week. Group by project and rank by impact."

**Self-audit.** Cron `0 10 1 * *`. Prompt: like the docs drift audit running on this very repo right now.

## Things to know

- **Routines don't write to chat sessions.** Output goes to `routine-id:<id>`, not `chat:<slug>`. To see results, look at the Routines page or search the lake by `routine-id`.
- **One spec per routine, immutable history.** Past fires and summaries persist forever. The "current" routine is always the latest spec delta with that id.
- **Permission mode matters.** `auto` runs Claude unattended (the classifier decides). `normal` requires a human at the kitty window approving each tool. Pick `normal` only when you'll be there to babysit.
- **The agent host runs the routine.** If your only agent is on a desktop that you turn off at night, a 7am routine won't fire until the desktop wakes up. Pair an always-on host (server, NAS) for routines that need to be reliable.
