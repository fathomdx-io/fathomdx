---
title: How to set up a routine
description: Schedule a prompt to run on a cadence. The witness decides whether to dispatch claude-code, write a feed card from substrate, fire an alert, or do nothing — depending on what the prompt actually asks for.
audience: developer
quadrant: how-to
last_verified: 2026-04-30
owners: [api/routine_scheduler.py, api/loop/witness.py, addons/agent/plugins/kitty.js, reference/routine-spec.md]
---

# How to set up a routine

A routine is **a prompt + a schedule**. When the time comes, Fathom's River (the witness) reads it like any other intent and decides what to do — fetch data with claude-code, synthesize a feed card from the lake, fire an alert, reply in chat, call a tool, stay silent. The route is a routing decision, not part of the routine spec.

This is a recent shift. Routines used to be a direct claude-code trigger; now they fire INTO the River and the River dispatches. See [What a routine can touch](#what-a-routine-can-touch) below for what changed.

This page covers two paths: ask Fathom to draft a routine for you in chat, or build it yourself in the dashboard. Both produce the same artifact (a spec delta).

## Prerequisites

The prerequisites depend on what your routine asks for. The minimum is **none beyond a running Fathom**: a routine that says "summarize this week's lake activity into a feed card" needs no agent, no kitty, no claude-code — the witness handles it from substrate.

You only need the agent stack when the routine asks for fresh data, file work, or anything outside the lake:

- A paired agent on the machine you want claude-code to run on (see [tutorial 3](../tutorials/03-fathom-does-things.md) or [pair another machine](./pair-another-machine.md)).
- kitty installed and on the agent's `PATH`.
- Claude Code installed and authenticated on the same machine.

Without an agent, "check the news" routines won't fetch anything — the witness will see no claude-code-capable host available and write a feed card from whatever's already in the lake (probably "I don't have fresh news; the last update I have was…").

## What a routine can touch

The witness reads a `routine-due` intent and picks a route the same way it does for a user-typed message. Available routes:

| Route | When the witness picks it | Side effects |
|---|---|---|
| `claude-code:<host>` | The prompt asks for fresh data, file edits, shell commands, or anything outside the lake. Trigger phrases: "check", "fetch", "look up", "go get", "what's new", "run X". | Spawns a claude-code session on the named host. Closure feeds back into the witness for synthesis. |
| `feed-card` | The prompt asks for a synthesis from substrate already in the lake. "Summarize this week", "what changed since Monday", "remind me of yesterday's wins". | One feed card lands. No external work. |
| `chat-reply` | The prompt is conversational, the answer is in memory. "Daily check-in: how's the mood?" | Renders as a chat-style message. |
| `alert:<level>` | The prompt asks Fathom to flag something if a condition is met. "If a research thread has been quiet 3+ days, surface it." | Pinned alert at top of feed. |
| `tool:<name>` | The prompt asks for a state change Fathom should propose for approval. "Once a month, propose a routine cleanup." | Proposal card with Edit/Deny/Approve buttons. |

Some implications:

- **A routine doesn't need an agent or claude-code.** If the work is "synthesize from the lake," the witness handles it directly.
- **A routine prompt that mixes fetch + synthesis splits naturally.** Tick 1: witness dispatches claude-code for the fetch. Tick 2 (when claude returns): witness synthesizes the user-facing card. The "synthesize into a concise update" instruction in your prompt is honored on Tick 2, in Fathom's voice — not as part of the claude-code session.
- **You don't pick the route.** The witness does, based on the prompt. Write the prompt as a request to Fathom, not as instructions for claude-code.

## Trust model: what claude-code can actually do (when the witness picks it)

If your routine routes through `claude-code:<host>`, that's **a full Claude Code session running as you** on the agent's host. Same shell environment, same `~/.claude/` config, same MCP servers, same authenticated tokens. Same filesystem. Same git and SSH credentials. The kitty window is not a sandbox.

Implications worth seeing clearly before writing one that runs unattended:

- A routine prompt that says "clean up old log files" can, depending on `permission_mode` and what the classifier thinks is safe, actually delete files. There is no per-routine permission scope.
- If your authenticated `gh` CLI can push to a repo, the routine can. If your `kubectl` context points at production, the routine has it.
- Helpers, MCP tools, and any API key the agent host has access to are all in scope.

If the witness picks `feed-card` or `chat-reply`, none of this applies — the work happens inside the api process and only writes feed deltas.

### `auto` vs `normal` (claude-code path only)

| Mode | Behavior | Right when |
|---|---|---|
| `auto` | Claude runs with `--permission-mode auto`. The classifier auto-approves "safe" actions and blocks "risky" ones. No human in the loop. | Read-mostly work where the worst case is "wrong summary," not "wrong rm". |
| `normal` | Each tool use prompts in the kitty window. A human approves every one. | You'll be at the machine when it fires, and the routine touches things you want to vet. |

The non-obvious failure modes:

- **`auto` is not "safe."** The classifier is a heuristic. Read prompts you write for `auto` mode the way you would read a prompt you were about to send to a coworker with admin access on your machine.
- **`normal` mode + unattended host = stall forever.** The kitty window opens, claude reaches the first tool prompt, and waits. Don't pick `normal` for a 3am routine.
- **There is no per-routine permission scope.** The scope is whatever Claude Code can do on that host, period.

### Per-host kill switch (claude-code path only)

The agent on each machine has an `allowed_permission_modes` config (defaults to `["auto", "normal"]`). Set this in `~/.fathom/agent.json` under the `kitty` plugin block to lock things down per-host: `["normal"]` for human-in-the-loop only, `[]` to disable claude-code execution entirely on that host.

### Host pinning

A routine spec can include a `host: <agent-name>` field. When set, only the agent whose `host` matches will spawn the claude-code window — other agents silently ignore the dispatch. Without it, the routine is fleet-wide and the witness picks any available host.

Pin a routine to a host when the work is host-specific ("clean up `~/Downloads` on my laptop"), the host has tools or credentials others do not, or you want it on a specific machine for reliability.

For non-claude-code routes (`feed-card`, `chat-reply`, etc.), `host` is informational — the work happens server-side regardless.

## Writing the prompt

Routines follow a four-section schema. Each section has a header (`# Purpose`, `# Needs`, etc.); the witness reads them as conventions. Freeform prose works too, but structured prompts give cleaner signal — same reason a good email has a subject and body.

```
# Purpose
[One sentence — what I'm trying to accomplish.]

# Needs
[What this needs to actually run — claude-code on a host, a tool,
or "substrate only" if the lake already has the data.]

# Steps
[The instructions — what to look for, what to filter, what to compare.]

# Ending
[How you want to be notified. Plain language. Witness reads this to
pick the route — card, DM, alert, silent, or something else.]
```

The full reference for each section, including more `# Ending` patterns, is in [routine-spec.md](../reference/routine-spec.md#writing-the-prompt). The dashboard's New Routine form prompts you for each section with a small form; you can write freeform if you prefer.

### The four notification choices

When you create a routine in the dashboard, you'll see:

> **How would you like to be notified when this is done?**
> ◯ A card in the feed
> ◯ Direct message to me
> ◯ Do something else: ____________________
> ◯ Do nothing

Each maps cleanly to a witness route. Pick one and the form fills `# Ending` for you. Add a free-text refinement underneath to layer on conditions ("but escalate to soft alert if anything major lands").

### Pattern 1 — recurring fetch + alert on threshold (Gold-to-Mac)

```
# Purpose
Track when gold's purchasing power crosses one Mac.

# Needs
claude-code on myras-fedora-laptop — live price fetch.

# Steps
1. Fetch gold spot price (Kitco).
2. Fetch refurbished 128GB iPhone 15 Pro Max price (Apple refurb).
3. Compute ratio. Compare to last fire's ratio in the lake.

# Ending
Stay silent on quiet days. If the ratio drops to 1.0 or lower (gold has
caught up to a Mac), send me a hard alert. Lead with the ratio + delta.
```

What happens: cron tick → `routine-due` intent. Witness reads `# Needs` and picks `claude-code:<host>` for the fetch. Claude-code returns prices. Next witness tick reads `# Ending`, evaluates the ratio condition, picks `silent` if not crossed or `alert:hard` if crossed.

### Pattern 2 — fetch + synthesize into a card (News briefing)

```
# Purpose
Morning news briefing — Trump health, AI/robotics, STL events.

# Needs
claude-code on myras-fedora-laptop — web fetch.

# Steps
1. Check world, national, and St. Louis news.
2. Filter for: Trump health changes, AI/robotics breakthroughs, STL events.
3. Surface only what's new since last fire.

# Ending
Card most days. Soft alert if anything genuinely major breaks (Trump
health change, AI breakthrough, STL emergency). Stay silent if literally
nothing new.
```

The synthesis instruction lives under `# Ending` but is executed by the *witness*, not claude-code. That's the key change from the old architecture: claude-code fetches the data, the witness writes the card.

### Pattern 3 — pure substrate synthesis (Weekly retro)

```
# Purpose
Weekly look-back at what landed in the lake.

# Needs
Substrate only — no claude-code needed.

# Steps
1. Pull what landed in the lake this week (commits, vault entries, chats).
2. Group by theme.
3. Surface one thing worth remembering next month.

# Ending
Send me a card. Three sections, one paragraph each.
```

No claude-code spawned. No agent needed. The lake already has the data; the witness composes the card directly.

### Pattern 4 — proposal cards (Routine cleanup)

```
# Purpose
Once a month, prune routines that aren't pulling their weight.

# Needs
Substrate only — read recent routine summaries from the lake.

# Steps
1. Look at every routine's last 5 summaries.
2. Flag any whose outputs were thin or didn't advance the work.

# Ending
For each candidate, propose disabling it. Show me the routine name and
why you flagged it. I'll approve or deny each.
```

Witness writes `tool:routines` proposal cards (one per candidate) with `action:update` `enabled:false`. Edit/Deny/Approve in the feed. Nothing changes until you approve.

### What to put under `# Steps`

Write the steps as if you're asking Fathom to do something — first person, conversational. Don't pre-script claude-code's tool calls; the witness will compose those if it picks claude-code.

Good:

```
# Steps
1. Fetch the price of gold and BTC.
2. Compare to yesterday's close.
```

Bad:

```
# Steps
You are claude-code. Run `curl https://...` to fetch gold.
Then run `curl https://...` for BTC. Call `fathom delta write` with
tags `[market, daily]`. Then exit.
```

The witness needs your *intent*, not your implementation. Tool calls and lake writes are downstream of the route choice — Fathom composes them based on what `# Needs` says it has access to.

### The four-beat structure (still applies)

The most common failure mode for a recurring routine is that it spins in place. Every fire it re-orients, re-summarizes, and exits without advancing. Avoid this with a four-beat prompt structure:

1. **Orient on what's done.** Search the lake for prior fires of this routine — what has past-you accomplished? What was the last clear next-step pointer?
2. **Decide the next step.** From where you are, what is the single most useful thing this fire?
3. **Do that one thing.** One unit of forward motion per fire.
4. **Leave a pointer for the next round.** Name what next-fire-you should pick up.

A prompt that bakes this in might end:

> Before you stop: check what prior fires of this routine have produced. Confirm you've moved past where the last one left off. Then state — out loud, so it gets captured — what the next fire should accomplish.

This is what separates a routine that compounds from one that just generates noise.

## Path A: ask Fathom to draft it

This is the easier flow. Open a Fathom chat session and describe what you want:

> Set me up with a routine that summarizes my GitHub notifications each weekday at 7am and posts a one-paragraph briefing.

Fathom drafts a routine. You see a proposal card with Edit / Deny / Approve buttons. Edit changes any field; Approve writes the spec delta. The scheduler picks it up within a poll cycle (60s by default).

## Path B: build it yourself in the dashboard

Open the dashboard's **Routines** page. Click **New routine**. Fill in:

| Field | What to put |
|---|---|
| **Name** | Human-readable label. Shown in the dashboard. |
| **Schedule** | A 5-field cron string. See cron examples below. |
| **Workspace** | Path to a directory on the agent host. Only used if the witness picks claude-code. Use `~/Dropbox/Work/your-project` or similar. |
| **Host** | Pin to a specific agent (or leave blank for fleet-wide). |
| **Permission mode** | `auto` or `normal`. Only used if the witness picks claude-code. |
| **Single fire** | If true, the spec is tombstoned after the first fire. |
| **Prompt** | What you want Fathom to do. Written conversationally. |

Save. The scheduler picks up the spec within 60 seconds.

### Cron expressions

| Schedule | Cron |
|---|---|
| Every weekday at 7am | `0 7 * * 1-5` |
| Every Saturday at 9am | `0 9 * * 6` |
| First of every month at 10am | `0 10 1 * *` |
| Every 4 hours | `0 */4 * * *` |
| Every hour on the hour | `0 * * * *` |

Cron is evaluated in the API container's local timezone (see `TZ` in `.env`).

## Test the fire manually

Click **Fire Now** on the routine's detail page. The routine fires into the River (writes a `routine-due` intent + a `routine-tick` marker), the witness deliberates on the next tick, and you'll see whatever it decided to do — a feed card, a claude-code dispatch, an alert, a chat-reply, or silence — appear in the dashboard feed within a few seconds.

There's no "skip the River" override. Routines fire into Fathom; Fathom decides what to do with them.

## What gets captured into the lake

A routine writes durable artifacts on every fire (cron, Fire Now, or witness-initiated — they're the same path now):

**Always**:
- `routine-due` intent in the puddle (kind:routine-due, body = your prompt). The witness reads this on its next tick.
- `routine-tick` marker in the lake (durable receipt; hydration only).
- Witness output — the feed card / chat-reply / claude-code dispatch / alert / proposal that the witness produced.

**When the witness picks claude-code**:
- A claude-code dispatch card (the kitty plugin spawns the session).
- The closure delta when claude returns (`task-complete`).
- A second witness tick that synthesizes the user-facing card.
- Anything fathom-connect captures inside the session (prompts, replies, tool calls — all auto-captured if `~/.claude/settings.json` has the hooks installed).

## Edit a routine

Routines edit by writing a new spec delta with the same `routine-id:<id>` tag. The scheduler always uses the latest spec by timestamp. The dashboard's **Routines** page does this for you when you save changes.

You can also edit by hand:

```bash
curl -X PUT 'http://localhost:8201/v1/routines/<routine-id>' \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{"schedule": "0 8 * * 1-5", "enabled": true, ...}'
```

The full field list is in [routine-spec.md](../reference/routine-spec.md).

## Pause a routine

Set `enabled: false` on the spec. Re-enable later by setting `enabled: true`.

## Delete a routine

Set `deleted: true` on the spec. The dashboard hides it. The scheduler skips it. History stays in the lake.

## Common patterns

**Daily briefing (claude-code routed).** Cron `0 7 * * 1-5`. Prompt: "Check overnight emails, GitHub notifications, and any new feed items tagged urgent. Synthesize what needs my attention this morning."

**Weekly retrospective (substrate-only).** Cron `0 17 * * 5`. Prompt: "Look at what's landed in the lake this week. Group themes. Surface one thing I'd want to remember next month."

**Self-audit (proposal-routed).** Cron `0 10 1 * *`. Prompt: "Once a month, look for routines whose last 5 summaries were thin or didn't advance the work. If you find any, propose disabling them."

**Quiet check (alert-routed).** Cron `0 9 * * *`. Prompt: "If any research thread has been quiet for 3+ days, surface it as a soft alert. Otherwise, stay silent."

## Things to know

- **Routines don't write to chat sessions.** Cron-driven activity goes to `routine-id:<id>`, not `chat:<slug>`. To see results, look at the Routines page or search the lake by `routine-id`.
- **One spec per routine, immutable history.** Past activity persists forever. The "current" routine is always the latest spec delta with that id.
- **The agent host runs claude-code (when the witness picks it).** If your only agent is on a desktop you turn off at night, a 7am routine routed through claude-code won't fetch until the desktop wakes. Substrate-only routines (feed-card, chat-reply, alert) don't need an agent.
- **`single_fire` is honored.** The scheduler tombstones the spec after the first cron tick fires.
- **`interval_minutes` is dead.** Use `schedule` (cron). The field is parsed for back-compat but ignored by the scheduler.
