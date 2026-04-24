---
title: "Tutorial 1: The lake and you"
description: Install Fathom, set up your profile, have your first chat, add your first source, and watch everything you just did converge into one searchable memory. About 30 minutes.
audience: developer
quadrant: tutorial
order: 1
last_verified: 2026-04-24
owners: [addons/scripts/install.sh, QUICKSTART.md, api/routes/]
---

# Tutorial 1: The lake and you

Welcome. This is the first of three tutorials that walk you through Fathom from zero. By the end of this one, you'll have a running lake on your machine, your profile in it, a first chat in it, and a first source feeding deltas into it. More importantly, you'll have seen all of those things show up together in a single search, and that's the moment where the idea of the lake stops being abstract.

Plan for about 30 minutes. Do it in order. Don't skip around.

## What you'll need

- A Linux, macOS, or Windows-with-WSL machine you can run Docker on.
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) or Podman with the compose plugin installed.
- An API key from one LLM provider: [Google AI Studio](https://aistudio.google.com/) (free tier is fine), OpenAI, or a local Ollama install.

You don't need to know anything else about Fathom yet. We'll build the picture as we go.

## Step 1: Install

One command. Paste this into a terminal:

```bash
curl -fsSL https://fathomdx.io/install.sh | bash
```

What this does:

1. Clones the fathomdx repo into `~/.fathom/src` (overridable with `FATHOM_DIR=...`).
2. Runs a preflight script that creates `~/.fathom/mind/` (where your lake's state will live) and generates a `.env` file.
3. Offers to start the Docker stack for you.

When preflight asks for your LLM provider and API key, enter them. When it asks whether to start the stack, say yes.

Give it a minute. The first boot pulls the Postgres image and builds three local images. When it's done, Fathom is listening on these ports:

| URL | What |
|---|---|
| `http://localhost:8201` | API and dashboard. This is where you go. |
| `http://localhost:4246` | Delta-store (the lake's internal HTTP API). |
| `http://localhost:4260` | Source-runner (external source poller). |

Everything is bound to 127.0.0.1 by default, so it's only reachable from your own machine.

Verify with one curl:

```bash
curl http://localhost:4246/health
# {"status":"ok"}
```

Then open `http://localhost:8201` in your browser.

## Step 2: Set your profile

The dashboard opens to a welcome state. Before you start using it, tell Fathom who it's talking to. Find the profile section (look for your avatar or "Your profile" in the sidebar) and fill in:

- **Your name.** What Fathom should call you.
- **Short bio.** A couple of sentences about what you do, what you're working on, what you care about. Fathom uses this as context in every turn.

Save it.

Look at what just happened. Your name and bio weren't stored in a user profile table. They landed in the lake as deltas, tagged `contact:<your-handle>` and `profile`. They are memories about you, stored the same way every other memory gets stored.

Verify: open a new terminal and hit the deltas API directly.

```bash
curl 'http://localhost:8201/v1/deltas?tags_include=profile&limit=5'
```

You'll see your profile come back in the result. The lake has no distinction between "profile data" and "everything else." This matters. It means that later, when you ask Fathom "what do I do for work?", the answer is already in the same substrate as your chats and your sources.

## Step 3: First chat

From the dashboard sidebar, open **Chat** and start a new session.

Say hello. Ask Fathom what it knows about you. It'll answer using your profile, because it can recall the profile deltas you just wrote. It might also tell you what tools it has available, or what kinds of things you can ask about.

Keep going for a few turns. Try:

- *"What should I ask you about?"*
- *"How do you remember things?"*
- *"Tell me about yourself."*

Each time you press send, two things happen. Your message becomes a delta in the lake, tagged with the session ID and `participant:user`. Fathom's reply becomes another delta, tagged the same way with `participant:fathom`. The conversation is not stored in a messages table somewhere; it *is* those deltas.

Verify again:

```bash
curl 'http://localhost:8201/v1/deltas?tags_include=chat&limit=10&sort=created_at:desc'
```

Your turn and Fathom's reply come back, most recent first. Each one is a distinct row. You could read them, search them, or combine them with any other query. They're not special.

## Step 4: Add a first source

Now let's give Fathom a source of information that isn't you typing. From the dashboard, find the **Sources** section and add a simple one.

The easiest to start with is an RSS feed. Pick a blog or news site you read; any RSS feed URL will do. Paste it in.

Behind the scenes, the source-runner service starts polling that URL on a schedule. Each time a new item appears in the feed, the runner writes it into the lake as a delta, tagged with the source name.

Wait a minute or two. A small feed might only have a handful of items on first poll; a busy feed will have dozens.

Verify:

```bash
curl 'http://localhost:8201/v1/deltas?source=rss&limit=5'
```

You'll see feed items come back. Each has a `content` field with the article text or summary, an `image_path` if the item had one, tags like `source:rss`, `feed:<url>`, and a timestamp.

Notice the shape is identical to your chat deltas and your profile deltas. Same fields, same table, same everything. The only difference is the tags and the source name.

## Step 5: Watch it converge

This is the part of the tutorial that justifies the other steps.

Go back to your chat session. Ask Fathom about something the source has been collecting. For example, if you added a feed from your favorite tech blog, ask:

*"What's been going on with [topic] lately?"*

If the blog has recent items on that topic, Fathom will find them in the lake, include them in context, and answer using them. The chat doesn't know or care that the answer came from the RSS source rather than another conversation. It searches the lake. Everything in the lake is eligible.

Now ask something that mixes sources:

*"Based on what I told you about my work, which of the recent posts are most relevant?"*

This is the convergence. Your profile (from Step 2), your conversation history (from Step 3), and the feed items (from Step 4) are all in the same substrate. A single query reaches all three. Fathom can cross-reference them because they're not in separate systems that need to be joined; they're in one system that never needed to know in advance that they were related.

Try a few more queries. Notice how Fathom can recall things you told it during chat earlier. Ask it about a feed item it mentioned a few turns ago. Ask it about your own bio. The lake holds all of it equally.

## What you just built

In about 30 minutes:

- A running fathomdx stack on your machine.
- Your profile in the lake as deltas.
- A chat session, with every turn in the lake as deltas.
- An RSS source polling in the background, writing deltas.
- A single substrate that contains all of the above and can be queried as one thing.

The word "lake" is not a metaphor. It's a Postgres table with an embedding column. Everything you just did became rows in it. The product of the tutorial is not the dashboard or the chat; it's the shape of the substrate that sits under them.

## Where to next

- **Tutorial 2: Fathom knows what's going on.** Wire up MCP and hooks so your Claude Code sessions read from and write into the same lake. After that, the machine and the chat share a brain.
- **Tutorial 3: Fathom does things.** Install the agent on your machine, add the heartbeat source, and ask Fathom to create its first routine. Memory is the floor; routines are the ceiling.

Or jump to how-tos when you have a specific goal:

- [Connect Claude Code](../how-to/connect-claude-code.md)
- [Run a second instance](../how-to/run-a-second-instance.md)
- [Add a feed source](../how-to/add-a-feed-source.md)

## If something didn't work

- **Install failed.** See the troubleshooting section in [QUICKSTART.md](../../QUICKSTART.md).
- **Dashboard won't load.** Give the API another 10 seconds after the stack starts. It waits on Postgres and delta-store. `docker compose logs api` shows what it's waiting on.
- **Chat isn't replying.** Check the API logs. Most likely the `LLM_API_KEY` in your `.env` is wrong or the quota is exhausted.
- **Source isn't ingesting.** The source-runner polls on a schedule; wait a few minutes. `docker compose logs source-runner` shows each poll attempt.

When you're ready, [Tutorial 2](./02-fathom-knows-whats-going-on.md) picks up where this one left off.
