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

## Trust model: what a routine can actually do

A routine is **a full Claude Code session running as you** on the agent's host. Same shell environment, same `~/.claude/` config, same MCP servers, same authenticated tokens. Same filesystem. Same git and SSH credentials. The kitty window is not a sandbox — it's claude in a terminal, with your hands on the keys, except you are not the one typing.

Implications worth seeing clearly before writing one that runs unattended:

- A routine prompt that says "clean up old log files" can, depending on `permission_mode` and what the classifier thinks is safe, actually delete files. There is no per-routine permission scope.
- If your authenticated `gh` CLI can push to a repo, the routine can. If your `kubectl` context points at production, the routine has it.
- Helpers, MCP tools, and any API key the agent host has access to are all in scope.

### `auto` vs `normal` — what each actually does

| Mode | Behavior | Right when |
|---|---|---|
| `auto` | Claude runs with `--permission-mode auto`. The classifier auto-approves "safe" actions and blocks "risky" ones. No human in the loop. | The routine is read-mostly, summarizes, writes deltas — work where the worst case is "wrong summary," not "wrong rm". |
| `normal` | Each tool use prompts in the kitty window. A human approves every one. | You will be at the machine when it fires, and the routine touches things you want to vet (file edits, network calls, shell). |

The non-obvious failure modes:

- **`auto` is not "safe."** The classifier is a heuristic. Read prompts you write for `auto` mode the way you would read a prompt you were about to send to a coworker with admin access on your machine.
- **`normal` mode + unattended host = stall forever.** The kitty window opens, claude reaches the first tool prompt, and waits. No timeout, no auto-decline, no summary delta. The dashboard shows a fire with no paired summary until you walk over and approve, decline, or kill it. Do not pick `normal` for a 3am routine.
- **There is no per-routine permission scope.** You cannot say "this routine can read but not write" or "this routine can touch only `~/Dropbox/Work/foo`". The scope is whatever Claude Code can do on that host, period.

### Per-host kill switch

The agent on each machine has an `allowed_permission_modes` config (defaults to `["auto", "normal"]`). If a routine fires with a mode not in the list, the agent writes a `kitty-fire-blocked` receipt delta and refuses to spawn the window. Set this in the agent's local config (`~/.fathom/agent.json` under the `kitty` plugin block) to lock things down per-host: `["normal"]` to require human approval for everything that fires here, `[]` to disable routine execution entirely on that host. The routine spec doesn't know what each agent allows — it asks for a mode, and the agent decides whether to honor it.

### Host pinning

A routine spec can include a `host: <agent-name>` field. When set, only the agent whose `host` matches will spawn the window — every other agent silently ignores the fire. Without it, the fire is fleet-wide: whichever paired agent picks it up first runs it (in practice, that's almost always the only agent online).

Pin a routine to a host when the work is host-specific ("clean up `~/Downloads` on my laptop"), the host has tools or credentials others do not, or you want the routine on a specific machine for reliability ("this NAS is always on; my laptop sleeps").

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

## What gets captured into the lake

A routine writes three durable things by default:

1. The **spec delta** itself (what the routine is).
2. A **`routine-fire` delta** every time the scheduler ticks (when it ran).
3. A **`routine-summary` delta** the routine is asked, in the prompt footer, to write before exiting (one-line outcome, paired by `fire-delta:<fire-id>` for the dashboard).

There is a fourth, often-overlooked layer. If `fathom-connect` is installed on the agent's host, **every prompt and reply inside the kitty session is also captured**, via the `UserPromptSubmit` and `Stop` hooks in `~/.claude/settings.json`. The routine's reasoning, its tool calls, the findings it speaks aloud — all of it lands in the lake as deltas tagged with the session ID, the source, the workspace. This happens whether you ask for it or not.

Practical upshot: **for an information-gathering routine, "recite findings" is itself the durable artifact.** You do not have to call `fathom delta write` for findings to persist. Claude saying out loud "I noticed that X bridges to Y" is captured the same as Claude writing a delta about it. The summary delta the footer asks for is the dashboard linkage tag, not the substance.

This means you have a choice for what to put in the routine prompt:

- **Pure exploration** → "recite your findings" is enough. The hooks capture the recitation; future routines and chat sessions can recall it.
- **Curated artifact** → ask for an explicit `fathom delta write` with specific tags, when you want a single composed paragraph (not a session transcript) findable by tag.
- **Both** → recite freely as you go, then write one clean delta at the end summarizing what cohered.

## Writing routines that move the work forward

The most common failure mode for a recurring routine isn't a bug — it is that the routine spins in place. Every fire it re-orients, re-summarizes the current state, and exits without advancing. After a month you have thirty deltas of "here's where things stand" and zero progress on the underlying work.

The fix is a four-beat structure in the prompt:

1. **Orient on what's done.** Search the lake for prior fires of this routine (`recall --tags routine-id:<id>`, or semantic search on the topic). What has past-you, under this routine, actually accomplished? What was the last clear next-step pointer?
2. **Decide the next step.** From where you are, what is the single most useful thing to do this fire? If the next step isn't obvious, the *first* step is to make it obvious — investigate, narrow, scope.
3. **Do that one thing.** Don't try to do everything. One unit of forward motion per fire is the goal.
4. **Leave a pointer for the next round.** Before exiting, recite (or write a delta) that names what next-fire-you should pick up. Be concrete: "next round, draft the introduction" beats "continue working on the paper."

Without step 4, step 1 has nothing to find, and the routine resets every fire. Without step 1, step 4 has no audience. The two are a pair.

A prompt that bakes this in might end with something like:

> Before you stop: search the lake for prior fires of this routine and confirm you have moved past where the last one left off. Then state — out loud, so it gets captured — what the next fire of this routine should accomplish. One sentence. Leave a breadcrumb.

This is what separates a routine that compounds from a routine that just generates noise. It's especially load-bearing for open-ended research, writing, or any work where "done" is a horizon, not a checklist.

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

- **Routines don't write to chat sessions.** The fire and summary deltas go to `routine-id:<id>`, not `chat:<slug>`. To see results, look at the Routines page or search the lake by `routine-id`. (Hook-captured prompt/reply deltas during the routine session also don't carry a `chat:` tag.)
- **One spec per routine, immutable history.** Past fires and summaries persist forever. The "current" routine is always the latest spec delta with that id.
- **The agent host runs the routine.** If your only agent is on a desktop that you turn off at night, a 7am routine won't fire until the desktop wakes up. Pair an always-on host (server, NAS) for routines that need to be reliable.
