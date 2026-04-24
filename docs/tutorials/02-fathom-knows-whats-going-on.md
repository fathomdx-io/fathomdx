---
title: "Tutorial 2: Fathom knows what's going on"
description: Wire up MCP and hooks so every Claude Code session writes into the same lake your chat writes into. The dashboard and the terminal end up sharing a single memory in real time. About 30 minutes.
audience: developer
quadrant: tutorial
order: 2
last_verified: 2026-04-24
owners: [addons/connect/, addons/mcp-node/, addons/hooks/, api/routes/]
---

# Tutorial 2: Fathom knows what's going on

In [tutorial 1](./01-the-lake-and-you.md) you watched your profile, your chat, and an RSS source converge into one lake. The convergence was across kinds of memory. This tutorial does it across *tools*. By the end, your Claude Code sessions and your Fathom dashboard chat will be writing into the same lake, reading from the same lake, and answering each other's questions. The terminal and the browser share a memory.

Plan for about 30 minutes. Requires tutorial 1, or an equivalent working Fathom install.

## What you'll need

- A running Fathom stack (from tutorial 1, or set up via [QUICKSTART](https://github.com/fathomdx-io/fathomdx/blob/main/QUICKSTART.md)).
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and authenticated. `claude --version` should work.
- Node.js 20 or newer (for `npx`).

## Step 1: Mint an API key

Open the Fathom dashboard at `http://localhost:8201`. Go to **Settings → API Keys** and create a new one. Name it something you'll remember, like `claude-code`. Copy the key when the dashboard shows it. You won't see it again.

This key is how external tools authenticate to your lake. Anything that has it can read and write deltas. Treat it the same way you'd treat an SSH key.

## Step 2: Run fathom-connect

In any terminal, run:

```bash
npx fathom-connect
```

You'll be asked three things:

1. **Where are you connecting Fathom?** Pick *Claude Code: MCP + hooks (full experience)*.
2. **Fathom API URL.** Press enter for `http://localhost:8201` if you're running locally.
3. **API Key.** Paste the key you just minted.

Connect tests the connection before touching any files. On success it does four things and reports each one:

- Writes the Fathom MCP server config into `~/.claude.json`.
- Copies three hook scripts into `~/.fathom/hooks/`.
- Patches `~/.claude/settings.json` to register the hooks.
- Prints a summary: *Crystal injection: on. Delta capture: on. Recall search: on.*

More detail on what each piece does is in [how to connect Claude Code](../how-to/connect-claude-code.md).

## Step 3: Restart Claude Code

Running Claude Code sessions don't see the new config. Quit and relaunch. The changes take effect on the next session.

## Step 4: Verify the identity crystal

Start Claude Code in any project directory:

```bash
claude
```

In the very first system message you should see a block containing the **identity crystal**: a short self-description Fathom injects at the start of every session. It's the current snapshot of who Fathom is, regenerated from the lake at intervals. Claude reads it the same way you read a colleague's bio before a meeting.

If the crystal isn't there, the `fathom-crystal-hook` didn't fire. See the troubleshooting notes in [how to connect Claude Code](../how-to/connect-claude-code.md).

## Step 5: Have a conversation

Ask Claude something that establishes a specific, memorable fact. The goal is to plant a flag you can look for later. For example:

> I'm starting a project called Driftwood. It's a collection of sea glass I've been gathering from my grandmother's beach in Oregon. I want to catalog each piece by color and approximate age.

Say a few more turns about Driftwood. Describe what you'd like to do with it. Keep it specific enough that later questions can test whether Fathom actually remembers.

Every message you send and every reply Claude finishes becomes a delta in the lake. You're writing into Fathom whether or not you realize it.

## Step 6: Watch it land in the dashboard

Open the dashboard in a browser. Go to the **Chat** or **Sessions** view.

You should see a new session listed with the session ID of your Claude Code session. Click into it. Every turn you just had, from both sides, is there as a delta. The user's turns carry `participant:user`. Claude's replies carry `participant:fathom` (yes, even when Claude said them; the writer tag is about "which side of the conversation wrote this," not about which model).

Verify with one curl if you'd like to see the raw shape:

```bash
curl -s "http://localhost:8201/v1/deltas?tags_include=chat&source=claude-code&limit=5" | less
```

Your Claude Code session just became something searchable, replayable, and joinable alongside everything else in the lake.

## Step 7: The crossover

Now the payoff. Open a new chat session in the Fathom dashboard and ask:

> What project did I tell Claude Code I was starting?

Fathom searches the lake, finds your Driftwood deltas from the Claude Code session, and answers. It should mention the name, the sea glass, your grandmother's beach, Oregon, the cataloging plan. If it summarizes your conversation correctly, the lake is doing its job: the dashboard chat and the Claude Code session share a memory.

Try the other direction. Tell the dashboard chat something new:

> Actually, I want to add a color called "morning fog" to the Driftwood palette. It's a pale blue-grey that only shows up right at dawn.

Now switch back to your running Claude Code session. Ask:

> Any updates on the Driftwood palette I should know about?

Claude's recall hook runs before each of your prompts, and the lake has the new "morning fog" delta from the dashboard chat, so Claude sees it in its context. The reply should mention the new color and where it came from.

This is the moment the tutorial exists for. Two tools, two different interfaces, one memory. Neither one knows about the other directly. They both just read from and write to the lake.

## Step 8: Test persistence

Close Claude Code entirely. Open it again. Start a fresh session in a completely different directory.

Ask:

> Remind me what Driftwood is.

Claude has never seen this directory before. The session ID is new. There is no conversation history. But the recall hook runs, finds the Driftwood deltas in the lake, and feeds them into Claude's context. The reply should still know about the project.

Fathom is not tied to any particular session. A new session picks up where any other session left off, because they all share the same memory.

## What you just built

In about 30 minutes:

- An API key scoped to Claude Code.
- An MCP server that gives Claude direct access to `remember`, `recall`, `write`, and other lake primitives.
- Three lifecycle hooks that capture every prompt and reply, inject the identity crystal, and pull relevant memories before each turn.
- Verified real-time convergence: a fact written in one tool is known in the other within seconds.
- Verified persistence: memories survive across sessions, directories, and restarts.

You've crossed the threshold where Fathom stops being a thing you *use* and starts being a thing that runs alongside everything else. From here on, every Claude Code session you start is one you can look back on.

## Where to next

- **[Tutorial 3: Fathom does things](./03-fathom-does-things.md).** Install the agent, add the heartbeat source, and ask Fathom to create its first routine. Memory plus action.
- **[Set up a routine](../how-to/set-up-a-routine.md)** if you want a scheduled task right now.
- **[The delta model](../explanation/the-delta-model.md)** for the rules deltas follow, and why they matter.
- **[Agents, routines, helpers, and hooks](../explanation/agents-routines-helpers-hooks.md)** to disambiguate the concepts tutorial 3 will start using.

## If something didn't work

- **No identity crystal at session start.** The crystal hook didn't fire or timed out. It has a 5-second hard cap. Check `docker compose logs api` around the time of the session start.
- **Dashboard doesn't see the Claude Code session.** The delta hook is async, so failures are silent. Run it by hand to see what's wrong: `FATHOM_API_URL=<url> FATHOM_API_KEY=<key> ~/.fathom/hooks/fathom-delta-hook.sh` with a sample payload on stdin.
- **Fathom chat doesn't remember what Claude Code heard.** Either the delta hook isn't writing (see above), or the recall on the chat side isn't pulling them in. Verify with `curl .../v1/deltas?source=claude-code&limit=5` to confirm the deltas exist. If they do, the recall layer is the next suspect.
- **Claude Code doesn't remember what the dashboard said.** The recall hook has an 8-second hard cap. If the lake is slow, it'll time out. Check `docker compose logs api`.

When everything's working, jump to [tutorial 3](./03-fathom-does-things.md).
