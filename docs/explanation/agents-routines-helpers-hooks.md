---
title: Agents, routines, helpers, and hooks
description: Four Fathom concepts that sound similar and do different things. Here's what each is, when to reach for it, and how they compose.
audience: developer
quadrant: explanation
last_verified: 2026-04-24
owners: [addons/agent/, addons/connect/, addons/hooks/, api/routes/]
---

# Agents, routines, helpers, and hooks

These four words get confused constantly. Each has a specific job. If you're adding behavior to Fathom, picking the right one is usually most of the design problem.

Short version:

| Thing | What it is | When to reach for it |
|---|---|---|
| **Agent** | A long-running daemon on a host machine | You want Fathom to have a presence on a specific machine, running plugins and executing routines |
| **Routine** | A scheduled prompt that fires into a Claude Code session | You want a task performed at a cadence (daily, hourly, Mondays) |
| **Helper** | A named capability Fathom can call | You want chat or a routine to be able to do a specific thing on demand (fetch weather, summarize a URL, draft text) |
| **Hook** | A shell command fired on a lifecycle event | You want to capture something that's happening in another tool (Claude Code, an IDE) as deltas |

## Agent

The agent is `addons/agent/`. One instance runs on each machine you want Fathom to know about. It's a Node daemon that:

- Heartbeats into the lake at a steady cadence, so the server knows the machine is online.
- Runs a collection of plugins (heartbeat, sysinfo, kitty, local-ui, vault, homeassistant). Each plugin owns a category of deltas it's responsible for writing.
- Executes routines when the server schedules them, by spawning a kitty window and launching Claude Code inside it.

An agent is bound to a host. If you have three machines you want in Fathom, you pair three agents. Pairing is a one-time operation that exchanges a short-lived pair code for a long-lived API key, stored at `~/.fathom/agent.json` on the machine.

Agents don't "do work" in a general sense. They run the plugins and routines you've configured. Think of the agent as the body Fathom has on your laptop: always there, doing the little things it was asked to do, reporting back.

## Routine

A routine is a scheduled prompt. You write the prompt in the dashboard, pick a cadence (cron expression, or "daily at 8am," or "Mondays at 6pm"), and save it. When the time comes, the routine-id fires as a `routine-fire` delta. An agent sees the fire, spawns a kitty window on its machine, starts `claude` inside, and injects the prompt.

Claude Code then does whatever the prompt asked for. The session it runs in can write back to the lake, read from it, invoke tools, open files. When the routine finishes, a summary delta lands in the lake paired to the same routine-id.

Routines are independent of chat sessions. A routine doesn't write into `chat:<slug>`. Its output lives under `routine-id:<id>`. You look at routine history on the routines page or by searching that tag.

Routines require an agent paired on a machine with both [kitty](https://sw.kovidgoyal.net/kitty/) and [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and authenticated. The agent is what reifies the schedule into an actual running process.

## Helper

A helper is a named capability Fathom can invoke during an inference turn. "Fetch the weather." "Summarize this URL." "Draft a response to this email." "Query a database." Each helper has a name, an input schema, and a runtime that executes it.

The lineage is deliberate: helpers are what lets a chat turn or a routine actually *do* something beyond speaking. A helper invocation during chat might look like:

1. User asks "what's the weather tomorrow?"
2. Inference turn decides to call the `weather` helper.
3. Helper runs, fetches the forecast, returns it.
4. Inference continues with the helper's result in context.
5. A delta is written capturing the helper call and its result, so the lake remembers what was asked and what was returned.

Helpers purposefully generate deltas as a side effect of running. That's the design. A weather helper writing a delta for each forecast it fetches means the next time someone asks "what did you tell me about tomorrow's weather yesterday?" the lake can answer.

Helpers compose with everything else. A routine can invoke helpers. A chat turn can. A hook can. They're the verb library.

## Hook

A hook is a shell command that fires on a lifecycle event in another tool, with the job of writing deltas into the lake.

The concrete example: Claude Code emits lifecycle events (`UserPromptSubmit`, `Stop`, `SessionStart`). If you install the `fathom-connect` hooks in a project, each of those events runs a small shell script that writes a delta. The result: every prompt you type into Claude Code and every reply Claude finishes gets captured in Fathom, tagged with the session ID, participant, source. The dashboard can replay any past Claude Code session because the lake holds every turn.

Hooks are the mechanism by which Fathom learns what's going on in tools it doesn't own. Claude Code is the current primary case. In principle, any tool that emits hookable events (IDEs, terminal multiplexers, shells) could be wired up the same way.

Hooks are not agents. They don't run continuously. They fire, write a delta, exit. The work they do is narrow by design: observe an event, record it, get out of the way.

## How they compose

A realistic flow that uses all four:

1. You pair an **agent** on your laptop.
2. You install **hooks** into a Claude Code project so every prompt and reply gets captured.
3. You create a **routine** that fires every morning at 8am, with a prompt like "check overnight GitHub notifications and summarize what needs my attention."
4. When the routine fires, the agent spawns kitty and runs Claude Code. The Claude Code session invokes the `github` **helper** to fetch notifications, summarizes them, writes a delta tagged `routine-id:<id>`.
5. The hooks capture every step of the Claude Code session so the lake has the full transcript, not just the summary.

That's four different pieces doing four different things, and because all of them write deltas into the same lake, the morning routine's output is searchable alongside your chat, alongside yesterday's routine, alongside anything else you've talked about with Fathom.

## Which one to reach for

- "I want Fathom on this machine." → Agent.
- "I want a task done on a schedule." → Routine (requires an agent).
- "I want chat to be able to do X." → Helper.
- "I want to capture what's happening in this other tool." → Hook.

When in doubt, the question to ask is: *does this need to run continuously, at a scheduled time, on demand, or in response to an event?* The four answers map to the four things.
