---
title: "Tutorial 3: Fathom does things"
description: Pair an agent on your machine, watch it heartbeat, ask Fathom to create its first routine, and see a scheduled task fire as a kitty window in front of you. Memory becomes action. About 30 minutes.
audience: developer
quadrant: tutorial
order: 3
last_verified: 2026-04-24
owners: [addons/agent/, addons/agent/plugins/, api/routes/pairing.py, api/routes/routines.py, reference/routine-spec.md]
---

# Tutorial 3: Fathom does things

The first two tutorials gave Fathom a memory ([T1](./01-the-lake-and-you.md)) and a presence in your tools ([T2](./02-fathom-knows-whats-going-on.md)). This one gives it a body. By the end, Fathom will have a paired agent on your machine, a heartbeat reporting health and presence, and a routine that fires on a schedule, runs Claude Code in a kitty window in front of you, and writes its results back into the same lake everything else lives in.

Plan for about 30 minutes. Requires tutorials 1 and 2.

## What you'll need

- Tutorials 1 and 2 done.
- Linux or macOS. Routines fire through the [kitty](https://sw.kovidgoyal.net/kitty/) terminal, which doesn't run on Windows. The agent itself works on any platform; routine execution does not.
- kitty installed and on your `PATH`. `kitty --version` should work.
- Claude Code installed and authenticated (you did this for T2).
- Node.js 20 or newer.

## Step 1: Pair the agent

Open the dashboard at `http://localhost:8201`. Find the **Agent** section in the sidebar. If nothing is paired yet, you'll see three OS tiles: Linux, Mac, Windows. Pick the one that matches the machine you're on.

A modal asks you to name the machine. Pick something short you'll recognize: `laptop`, `studio`, `home-server`. Letters, numbers, dots, dashes, and underscores are allowed. That name labels every delta the agent writes from here on.

The modal then shows a pre-filled command with a single-use pair code valid for ten minutes:

```bash
npx fathom-agent init --pair-code pair_<short-lived-code>
```

Copy and run it in a terminal. The agent installs from npm, redeems the pair code for a long-lived API key, writes the key to `~/.fathom/agent.json`, and starts heartbeating. The dashboard watches for the first heartbeat and closes the modal the moment it arrives. No refresh needed.

To keep the agent running in the foreground:

```bash
npx fathom-agent run
```

For something that survives reboots, `fathom-agent install` drops a systemd user unit on Linux, a launchd plist on macOS.

## Step 2: Watch it heartbeat

The agent writes one heartbeat delta every minute, plus deltas from each enabled plugin. Verify:

```bash
curl 'http://localhost:8201/v1/deltas?source=fathom-agent&tags_include=plugin:heartbeat&limit=5'
```

You'll see entries with tags like `agent-heartbeat`, `plugin:heartbeat`, `version:0.11.13`, and `host:<your-machine-name>`. Each one carries the current state of the agent and its plugins. If the dashboard's Agent section shows your machine as online, that's because of these.

This is presence. Fathom now knows whether your machine is running and reachable.

## Step 3: Tour what's running

A fresh agent comes with several plugins enabled by default. Each plugin is responsible for one category of observation:

| Plugin | Writes |
|---|---|
| `heartbeat` | One delta per minute with overall agent state. |
| `sysinfo` | CPU, memory, thermal, battery, disk, network. Roughly every minute on Linux; less often elsewhere. |
| `local-ui` | Metadata about the dashboard window when one is open. |
| `kitty` | Watches for `routine-fire` deltas and spawns kitty windows to execute them. Writes nothing on its own. |
| `vault` | If you point it at an Obsidian vault, writes a delta for every note change. Off by default. |
| `homeassistant` | If you have a Home Assistant instance, writes deltas for sensor and state changes. Off by default. |

Browse what your agent has written so far:

```bash
curl 'http://localhost:8201/v1/deltas?source=fathom-agent&limit=20' | less
```

You should see a mix of heartbeat and sysinfo entries by now. They are not noise. Asking Fathom "is my laptop running hot?" later this week is a query that lands directly on these deltas.

## Step 4: Draft a routine in chat

Routines are scheduled prompts. They live in the lake as spec deltas with a name, a cron schedule, and a prompt body. When their time comes, the kitty plugin spawns a window, runs Claude Code inside it, and injects the prompt.

Open the dashboard's **Chat** section. Start a new session and ask:

> Set me up with a routine that checks the local weather forecast each morning at 8am and tells me if anything stormy is coming. Call it Stormy Weather Alert.

Fathom drafts a routine for you: a name, a cron expression (`0 8 * * *`), a workspace, and a prompt body that asks Claude to check the forecast and report on stormy conditions. It shows you the draft and waits for your confirmation. Review it. If the prompt or schedule isn't quite right, ask Fathom to adjust.

When you confirm, a spec delta is written to the lake with tags `spec`, `routine`, and `routine-id:stormy-weather-alert` (or whatever id Fathom assigned).

## Step 5: Watch the scheduler pick it up

The lake scheduler polls for spec deltas every 30 seconds. After your routine is saved, give it a minute, then check:

```bash
curl 'http://localhost:8201/v1/deltas?tags_include=spec&tags_include=routine&limit=5'
```

Your routine spec is there. Open the dashboard's **Routines** page. Your new routine should appear in the list with its schedule, next-fire time, and current status.

You can wait until 8am tomorrow to see it fire, or you can fire it manually from the dashboard for the demo. Click the run-now action.

## Step 6: The fire

Two things happen in quick succession.

First, a `routine-fire` delta lands in the lake, written by the scheduler. Tags: `routine-fire`, `routine-id:<id>`, `workspace:<your-workspace>`. The kitty plugin on your agent sees it within a poll cycle.

Second, a kitty terminal window opens on your machine. You will see this happen. Inside the window, Claude Code starts up with the routine's prompt already injected. Claude does the actual work: queries weather data via whatever helper or tool it has access to, summarizes, and writes a `routine-summary` delta back into the lake.

When Claude finishes, the kitty window closes (or stays open for inspection, depending on configuration). The dashboard's Routines page pairs the fire with its summary using the `fire-delta:<fire-id>` tag.

## Step 7: Find the summary

```bash
curl 'http://localhost:8201/v1/deltas?tags_include=routine-summary&limit=5'
```

The summary is just another delta. Same shape as your chat turns, your sources, your heartbeats, your profile. The only thing distinguishing it is its tags.

Open a Fathom chat session and ask:

> What did you find about the weather this morning?

Fathom recalls the routine summary, treats it like any other memory, and tells you. The routine's output is now searchable, joinable, and combinable with everything else in the lake.

## Step 8: Memory plus action, in one substrate

Take stock of what's now true:

- A paired agent runs on your machine, heartbeating and reporting plugin state.
- A scheduled routine runs Claude Code in a kitty window on your behalf at 8am every day.
- The output of the routine is a delta. The agent's heartbeats are deltas. Your chats are deltas. Your sources are deltas. Your profile is a delta.

Across three tutorials, you've built up the lake from a single substrate that holds your memory (T1), to a substrate that two different tools share in real time (T2), to a substrate that Fathom can read, decide on, act upon, and write back into autonomously (T3).

Memory and action are not two systems Fathom has. They're one substrate Fathom uses for both.

## What you just built

- An agent paired on your machine, with heartbeat + sysinfo plugins writing observations every minute.
- A view, via curl or the dashboard, of your machine's current state as queryable deltas.
- A routine that fires daily on a cron schedule, runs Claude Code in front of you, and writes its result back to the same lake.
- A working picture of how memory and autonomous action share a substrate in Fathom.

## Where to next

You've finished the trilogy. From here, every other doc in this tree is goal-shaped:

- **[Set up another routine](../how-to/set-up-a-routine.md)** for things you want done regularly. Cleanup tasks, summaries, periodic checks.
- **[Pair another machine](../how-to/pair-another-machine.md)** to give Fathom presence elsewhere.
- **[Add a feed source](../how-to/add-a-feed-source.md)** to bring more outside information into the lake.
- **[Write a source plugin](../how-to/write-a-source-plugin.md)** when an existing plugin doesn't capture something you care about.

For the design rationale behind any of this, see the [explanation tree](../explanation/what-the-lake-is.md).

## If something didn't work

- **`npx fathom-agent init` fails to redeem the pair code.** Codes expire after 10 minutes. Re-mint one in the dashboard.
- **Agent heartbeats but the dashboard says offline.** Check `~/.fathom/agent.json` exists with a non-empty `apiKey` field. If not, re-pair.
- **Routine saves but never fires.** The lake scheduler polls every 30 seconds. Wait a minute, then check `docker compose logs api --tail 50` for scheduler activity. Verify the cron expression in the spec delta.
- **Routine fires but no kitty window appears.** kitty isn't on the agent's `PATH`, or kitty's remote-control protocol isn't enabled. Run `kitty --version` from the same shell the agent runs in. The agent passes the remote-control flags inline per spawn, so no `kitty.conf` setup is needed, but the binary has to be reachable.
- **Kitty window opens but Claude Code never starts.** The `claude` binary isn't on the agent's `PATH`, or you haven't authenticated. Run `claude --version` then `claude` and confirm it works manually before retrying the routine.
- **Routine completes but no summary delta lands.** Claude exited before writing its summary. Look at `docker compose logs api --tail 100` for the fire delta and any error trail. Most often this is an auth issue with the LLM provider; see [troubleshoot install](../how-to/troubleshoot-install.md).

When this works for you, you're done with the tutorial path. The how-tos and explanation pages take it from here.
