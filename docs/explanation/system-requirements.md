---
title: System requirements (and what Fathom isn't)
description: What you need to run a self-host. What it costs. What kinds of products this isn't.
audience: operator
quadrant: explanation
last_verified: 2026-04-24
owners: [docker-compose.yml, .env.example, addons/scripts/install.sh]
---

# System requirements (and what Fathom isn't)

Before you install, here's what's needed and what to expect. This page is partly about specs and partly about framing: Fathom is a particular shape of thing, and several products it sometimes gets confused with are not what it is.

## Hardware

Reasonable minimums for a single-user install:

- **CPU.** Anything from the last decade. The api and source-runner are I/O-bound; the embedder is the heaviest CPU consumer when re-embedding large batches.
- **RAM.** 4 GB usable for the stack, plus whatever your OS needs. 8 GB total is comfortable for a personal install.
- **Disk.** 5 GB to start (Postgres image, base data, room to grow). Active use adds a few hundred megabytes per month for typical lake activity (chat, a few RSS feeds, agent heartbeats). Image-heavy sources can grow it faster; plan for 50 GB if you're ingesting a lot of feed images.
- **Network.** Outbound to your LLM provider. If you're using a local model, no outbound needed for inference; outbound is still needed for source pollers (RSS, Mastodon, etc.).

Fathom also runs on a small VPS (1 vCPU, 2 GB RAM). It's tighter; expect the api to be slower under embedding bursts. For a long-lived install, more headroom is worth the small cost.

## Operating system

Linux, macOS, or Windows-with-WSL. The api stack runs in containers, so the host distro almost doesn't matter as long as Docker (or Podman) works.

For routines that route through claude-code (when the witness picks `claude-code:<host>` for fetch / file work / shell): Linux or macOS only. kitty doesn't run on Windows. Heartbeats, sources, MCP, and hooks all work on Windows-WSL. Substrate-only routines (those the witness routes to feed-card / chat-reply / alert / tool-proposal) need none of this and run inside the api container.

## Required software

- **Docker** or **Podman** with the compose plugin. Either works. The bundled `docker-compose.yml` is portable.
- **Git** (for the install script's clone step).
- **Node.js 20+** (for `npx fathom-agent`, `npx fathom-connect`).
- **Bash** (the install script and preflight are bash; macOS, Linux, WSL all have it).

Optional, depending on what you want to do:

- **kitty** for routines that route through claude-code (and for any other claude-code dispatch the witness emits).
- **Claude Code** for the MCP-and-hooks integration in [tutorial 2](../tutorials/02-fathom-knows-whats-going-on.md).
- **Obsidian** if you want the vault source plugin to ingest a notes vault.
- **Home Assistant** if you want the HA bridge to ingest sensor and state changes.

## What it costs to run

Two cost categories, and they're independent:

**Infrastructure (the host).** A laptop you already own, or a small VPS for $5-10/month. There's no Fathom subscription; the project is self-hosted by design.

**LLM provider.** Pick one:

- **Gemini** has a generous free tier; for a personal install with moderate use, you can stay in the free tier indefinitely.
- **OpenAI** bills per token. Typical personal use runs under $5/month for chat, maybe more if you have heavily-firing routines.
- **Anthropic** bills per token. Similar order of magnitude as OpenAI; pricing varies by model.
- **Local models** (Ollama, LM Studio, vLLM, llama.cpp) cost no API spend, just VRAM. A 16 GB GPU runs an 8B model comfortably; a 24 GB GPU runs a 32B model. Quality varies; small local models are weaker than current frontier APIs.

Mixed setups are common: chat on a local model (privacy), embeddings on Gemini (quality + free tier), occasional summarization on a stronger paid model.

## What Fathom is not

Several products people sometimes assume Fathom is. None of these are accurate:

- **Not a hosted service.** There's no `app.fathom.com`. You run it yourself; the source is the source.
- **Not a multi-tenant product.** One install per person. See [why Fathom is one mind per person](./why-fathom-is-one-mind-per-person.md).
- **Not a Slack or chat-app replacement.** The chat UI is for talking to Fathom, not for talking to other humans through Fathom.
- **Not an agent platform-as-a-service.** Fathom uses agents on your machines and via Claude Code; it doesn't host or sell agent capacity.
- **Not a wrapper around a single LLM.** Fathom can speak to multiple providers simultaneously and assigns tasks per slot. The lake is the substrate; LLMs are tools.
- **Not a knowledge graph.** Tags are conventions, not declared schema. There's no ontology layer; meaning emerges from tag usage and embeddings.
- **Not a backup tool.** Fathom holds your memory but isn't a backup of your other systems. If you're after "back up my Notion / Obsidian / email," that's a different product (though [add a feed source](../how-to/add-a-feed-source.md) can ingest some streams as deltas).

## What it asks of you

This is not a zero-maintenance product. Operating a self-host means:

- Updating it occasionally ([how to update Fathom](../how-to/update-fathom.md)).
- Backing up the lake ([how to back up and restore](../how-to/back-up-and-restore-the-lake.md)).
- Watching disk space if you ingest image-heavy feeds.
- Rotating tokens if a host gets compromised or someone leaves your trust circle.
- Picking and managing an LLM provider account.

If you're not comfortable with `docker compose logs` and editing a `.env` file, this is the wrong product for you right now. Fathom assumes a self-host operator who can do those things.

## What it gives you in return

A memory substrate that's yours. One mind, your shape, on your hardware. The trade you're making is operational complexity in exchange for ownership and privacy. Whether that trade makes sense depends on what you're using Fathom for.

## Things to know

- **Self-hosting isn't free.** It costs hardware time, attention, and a small amount of LLM-provider money (or no money + VRAM if you go local).
- **First-run friction is real.** The install one-liner handles most of it. [Tutorial 1](../tutorials/01-the-lake-and-you.md) walks the rest. If something breaks, [troubleshoot install](../how-to/troubleshoot-install.md) covers the common failure modes.
- **There's no upsell path.** No paid tier; no enterprise version; no "premium features." The whole product is what's in the repo.
- **The lake survives versions.** Updates change code, not data. Your install from today and an install you do six months from now will read the same lake without ceremony.
