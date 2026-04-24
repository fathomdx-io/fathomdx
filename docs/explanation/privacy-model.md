---
title: Privacy model
description: What lives where, what crosses the network, and where the trust boundaries are. Fathom is single-tenant by design; the threat model is "you operate it on your own machine."
audience: developer
quadrant: explanation
last_verified: 2026-04-24
owners: [docker-compose.yml, .env.example, api/routes/auth.py]
---

# Privacy model

Fathom is single-tenant by design. One person owns the install, runs the server, holds the API keys, and is the audience for everything in the lake. The privacy story falls out of that.

This page is about where data physically lives, what crosses what boundary, and what threats the architecture defends against versus what it doesn't.

## What lives where

Two on-disk locations carry your lake:

- **Postgres named volume** (`${COMPOSE_PROJECT_NAME}-pg`). Holds deltas, embeddings, sessions, contacts, indexes. Managed by Docker or Podman; cannot live on a syncing filesystem (Dropbox, OneDrive) because Postgres writes pages non-atomically and sync layers corrupt them mid-write.
- **`LAKE_DIR`** (default `~/.fathom/mind/`). Holds image moments referenced from deltas, backups, drift history, mood state, and API tokens.

The fathomdx code itself is at `~/.fathom/src/` (or wherever `FATHOM_DIR` points). The code doesn't carry your data; it processes it.

## Network exposure

Default bindings:

| Service | Port | Default binding |
|---|---|---|
| api | 8201 | 127.0.0.1 |
| delta-store | 4246 | 127.0.0.1 |
| source-runner | 4260 | 127.0.0.1 |
| postgres | 5432 | container only |

`127.0.0.1` means localhost only. Nothing reaches the lake from outside the host until you change that. To expose the api to your LAN, you edit the port mapping in `docker-compose.override.yml` (the bundled `.vm-example` is a starting point). To expose it to the internet, you put a reverse proxy in front (Caddy, Nginx, Cloudflare Tunnel) and you're now responsible for TLS and access control.

Fathom does not phone home. There is no telemetry, no analytics endpoint, no central registry of installs.

## Authentication

Three layers of token live in a default install:

- **Admin token.** The first contact bootstrapped during install. Unscoped admin access. Controls the dashboard.
- **Member tokens.** Created via self-signup (`/ui/onboarding.html` if `FATHOM_SIGNUP_ENABLED=true`) or admin-minted for additional contacts. Scoped to that contact's identity in the lake.
- **Agent tokens.** Created when you pair an agent (`npx fathom-agent init`). Long-lived; let the agent on a paired machine read from and write to the lake.
- **Integration tokens.** Created in **Settings → API Keys**. For external tools (Claude Code via `fathom-connect`, OpenAI-compatible clients eventually). Each one is scoped and revocable.

Tokens are stored in `LAKE_DIR/tokens/`. They're not in Postgres; they're files. Anyone with read access to that directory has read access to the lake. Treat the directory the way you'd treat an SSH key.

The internal `DELTA_API_KEY` env var is a separate concern: it gates direct access to delta-store from outside the api container. Leave it blank for single-user setups (the network binding already restricts access); set it for multi-host setups where delta-store gets exposed beyond localhost.

## What crosses the network outbound

Configurable, but the categories are:

- **LLM provider.** Whichever you've configured in `.env`. Your prompts and recalled context go to that provider. This is the largest privacy surface in the system; pick a provider whose data-handling matches your tolerance. Local models (Ollama, LM Studio) keep prompts on your machine.
- **Source pollers.** RSS feeds, Mastodon accounts, Home Assistant bridges, vault watchers; each only reaches what you point it at. The source-runner does outbound HTTP to those endpoints.
- **Agent updater.** `npx fathom-agent` checks npm for the current version when you run it. No data goes to npm beyond the package name.
- **Image fetches.** When a feed item references an external image, the runner downloads it once into `LAKE_DIR/images/`. That's the publisher's CDN seeing one request per image.

What does not cross outbound, ever:

- Your deltas (except as part of context to the LLM provider you've chosen).
- Your contacts list.
- Your session content.
- Telemetry of any kind.

## Multi-contact considerations

Fathom supports multiple human contacts in one lake. Each contact has a tag (`contact:<slug>`) and a token that's scoped to identify them. The most common case is "you and people you talk to": your spouse, a kid, a friend who occasionally drops in.

Contacts share the lake by default. There's no per-contact isolation built in. If your spouse is a contact in your Fathom and writes something into chat, that delta is in the same lake your other deltas are in, and recall sees it across contacts.

This is intentional. Fathom's design premise is that one person owns the lake; contacts are people that person knows, not separate accounts. If what you want is "two Fathoms, your data and theirs, walled off," run two separate stacks (see [back-up-and-restore the lake](../how-to/back-up-and-restore-the-lake.md) for the second-stack workflow).

## What the architecture defends against

- **Network attackers without local access.** Default 127.0.0.1 bindings; nothing reachable until you explicitly expose it.
- **Centralized data exposure.** There is no central server. A breach of fathomdx-io's infrastructure cannot leak your data because your data isn't there.
- **Vendor lock-in.** Postgres dump plus a tarball is a complete export. Schema is documented. Any other tool that can read pgvector can read your lake.
- **Silent telemetry.** Source code is auditable; nothing in the build sends data anywhere by default.

## What it does not defend against

- **Local compromise of your host.** Anyone with shell access on your machine has the lake.
- **Compromise of your LLM provider account.** The provider sees prompts and context. Their security is theirs.
- **Compromise of your reverse proxy or VPN.** When you expose Fathom beyond localhost, the access-control story is whatever you put in front of it.
- **Backups left in unencrypted storage.** A `pg_dump` and `LAKE_DIR` tarball uploaded to S3 without encryption is plaintext. Encrypt before transit if your backups land somewhere shared.
- **Agent-host compromise.** A paired agent has a token that reads and writes the lake. If a host is compromised, the token is compromised. Rotate (see [rotate an API key](../how-to/rotate-an-api-key.md)) after re-securing the host.

## Things to know

- **Fathom is single-tenant.** One install per person. See [why Fathom is one mind per person](./why-fathom-is-one-mind-per-person.md) for the design argument.
- **The LLM provider is the privacy boundary you choose.** Local models keep everything on-device; cloud providers see what you send them. Pick deliberately.
- **The lake is not encrypted at rest by default.** Postgres data and `LAKE_DIR` are plaintext on disk. Use full-disk encryption on the host if that matters to you.
- **Backups carry the same surface as the lake.** A backup that escapes is the same risk as a lake that escapes.
- **Tokens are bearer tokens.** Anyone holding one can do whatever the token's scope permits. There's no second factor by default.
