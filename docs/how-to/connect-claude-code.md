---
title: How to connect Claude Code to Fathom
description: Install MCP tools and lifecycle hooks so every Claude Code session reads from, and writes into, your lake.
audience: developer
quadrant: how-to
last_verified: 2026-04-24
owners: [addons/connect/, addons/mcp-node/]
---

# How to connect Claude Code to Fathom

One command, two minutes. After this, every Claude Code session you run has MCP tools for lake search and write, plus hooks that capture every prompt and reply as deltas. Past sessions become retrievable. The identity crystal injects into each session's context at start.

## Prerequisites

- A running Fathom stack reachable from your machine. If you're running locally, `http://localhost:8201` is the default.
- Claude Code installed and authenticated. `claude --version` should work.
- Node.js 20 or newer (for `npx`).
- An API key from Fathom. Get one from the dashboard: **Settings → API Keys → Create**.

## Run the connector

From any terminal (the directory doesn't matter; connect writes to your home config):

```bash
npx fathom-connect
```

You'll be prompted through three questions:

1. **Where are you connecting Fathom?** Pick *Claude Code: MCP + hooks (full experience)*.
2. **Fathom API URL.** Press enter to accept `http://localhost:8201`, or paste your remote URL.
3. **API Key.** Paste the key you minted in the dashboard.

Connect tests the connection before touching any files. If the test fails you'll see the reason (unreachable URL, invalid key, wrong port) and nothing gets written.

On success, connect does four things:

- Writes the Fathom MCP server into `~/.claude.json` (user-scope, applies to every Claude Code project).
- Copies hook scripts into `~/.fathom/hooks/`.
- Patches `~/.claude/settings.json` to register the hooks against Claude Code's lifecycle events.
- Prints a summary: *Crystal injection: on. Delta capture: on. Recall search: on.*

## Restart Claude Code

Running sessions don't see the new config. Quit and relaunch Claude Code, or start a fresh session. The changes pick up on the next start.

## Verify it's working

Start Claude Code in any project. In the very first system message you should see a block labelled with the identity crystal, the self-description Fathom injects at SessionStart. That confirms the `fathom-crystal-hook` is wired up.

Next, type any message. Then check the Fathom dashboard (`http://localhost:8201`) and open the **Chat** or **Sessions** view. You should see a fresh session whose ID matches the Claude Code session ID, with your message as the first delta. That confirms the `fathom-delta-hook` is wired up.

Finally, ask Claude something that requires memory: *"What did we talk about yesterday?"* If the recall hook is live, Claude's reply should reference past deltas by name or date, not hallucinate. That confirms the `fathom-recall-hook` is wired up.

## What the hooks do, briefly

Three hook scripts fire on Claude Code lifecycle events:

| Hook | Event | Purpose |
|---|---|---|
| `fathom-crystal-hook.sh` | `SessionStart` | Pulls the current identity crystal from the lake and injects it into the session's opening context. Synchronous with a 5-second timeout. |
| `fathom-recall-hook.sh` | `UserPromptSubmit` | Before each user prompt is sent, runs a recall against the lake for relevant memories and surfaces them to Claude. Synchronous with an 8-second timeout. |
| `fathom-delta-hook.sh` | `UserPromptSubmit`, `Stop` | Writes a delta for each user prompt and each assistant turn. Fires asynchronously so it never blocks. |

See [the delta model](../explanation/the-delta-model.md) for the rules deltas follow, and [agents, routines, helpers, and hooks](../explanation/agents-routines-helpers-hooks.md) for where hooks sit in the broader picture.

## Reconnecting

Safe to rerun. `npx fathom-connect` will overwrite the existing Fathom entries in both config files without disturbing anything else in them. Use this when you've rotated your API key, moved your Fathom server to a new URL, or want to re-install the hooks after a Claude Code config reset.

## Removing the connection

Manual cleanup:

1. Open `~/.claude.json` and delete the `mcpServers.fathom` block.
2. Open `~/.claude/settings.json` and remove any hook entries whose command contains `fathom-` or `.fathom/hooks/`.
3. Optionally remove `~/.fathom/hooks/` entirely.

Restart Claude Code. The session will run with no Fathom presence.

## Troubleshooting

**`Connection failed: Invalid API key`.** The key you pasted doesn't exist in the lake, or was revoked. Regenerate one in the dashboard.

**`Connection failed: ECONNREFUSED`.** The server isn't running at the URL you gave, or your network can't reach it. Test with `curl <url>/v1/stats` and debug from there.

**Session starts but the identity crystal doesn't appear.** The `fathom-crystal-hook` ran but returned nothing, or timed out. Check `docker compose logs api` around the time of the session start for errors. The 5-second timeout is a hard cap; a slow or unhealthy API will miss it.

**Deltas aren't landing in the lake.** The delta hook is async, so failures are silent by design. Run the hook by hand to see what's going wrong: `FATHOM_API_URL=<url> FATHOM_API_KEY=<key> ~/.fathom/hooks/fathom-delta-hook.sh` with a sample payload on stdin. The script's output will show the failure.

**I want to connect Claude Desktop or Cursor instead.** Rerun `npx fathom-connect` and pick *Claude Desktop / Cursor: MCP only*. Hooks aren't available in those hosts; you'll get MCP tools but not automatic delta capture.
