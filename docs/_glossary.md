---
title: Glossary
description: Vocabulary for fathomdx — what each term means, and where it surfaces in code and UI.
audience: developer
quadrant: reference
last_verified: 2026-04-24
owners: [api/, delta-store/, source-runner/, addons/]
---

# Glossary

fathomdx's internal vocabulary. Use these terms in docs, code comments, commit messages, and developer-facing UI. A few of these surface in the end-user dashboard under softer names; that's noted in the "Also shown as" column. Consumer-product vocabulary is otherwise out of scope for this tree — these docs are for developers and self-hosting operators.

## Core substrate

| Term | Meaning | Also shown as |
|---|---|---|
| **lake** | The postgres+pgvector delta store. Single shared substrate for all memory. Every write, every read, every search goes through the lake. | "Mind" (in the dashboard top-level page) |
| **delta** | One atomic memory entry. Immutable once written. Carries content, tags, source, timestamps, optional embedding, optional image. The unit of everything. | "moment" (in user-visible chat and dashboard copy) |
| **tag** | Convention for grouping and filtering deltas. Not a schema — any string works. Canonical tags are enumerated in [`reference/reserved-tags-spec.md`](./reference/reserved-tags-spec.md). | — |
| **embedding** | Vector representation of a delta's content. Powers semantic search. Stored in the same row as the delta. | — |

## Sessions and conversations

| Term | Meaning |
|---|---|
| **session (chat)** | A tag (`chat:<slug>`), not a table. Anyone who writes a delta into that tag is a member. The session IS the timestream of all such deltas. See [`../CLAUDE.md`](../CLAUDE.md#chat-sessions). |
| **participant** | Who wrote a delta into a session. Canonical values: `participant:user`, `participant:fathom`. |
| **chat-name delta** | A rename event for a session. Latest wins. Produces a human title for the dashboard sidebar. |

## Services (the compose stack)

| Service | Port | Role |
|---|---|---|
| `postgres` | 5432 | Lake storage. pgvector/pg17. Named volume `${COMPOSE_PROJECT_NAME}-pg`. Never a bind mount. |
| `delta-store` | 4246 | HTTP API over the lake. Write, query, embed, search, engage. |
| `source-runner` | 4260 | Polls external sources and writes deltas (RSS, Home Assistant, sysinfo, vault, etc.). |
| `api` | 8201 | Consumer API + dashboard UI. The public entry point. |

## Addons (host-side, not in compose)

| Addon | Role |
|---|---|
| `addons/agent` | Long-running daemon on a host machine. Runs plugins (heartbeat, sysinfo, kitty, localui, vault, homeassistant). Writes sensor and activity deltas. Executes routine fires. |
| `addons/cli` | Command-line client. Reads from and writes to the lake over HTTP. |
| `addons/mcp-node` | MCP server exposing `remember`, `recall`, `write`, `engage`, `see_image`, etc. to MCP clients (Claude Code, etc.). |
| `addons/connect` | Installs the MCP server + hooks into a Claude Code project. Bridges claude-code sessions into the lake as deltas. |
| `addons/hooks` | Shell hooks that fire on claude-code lifecycle events (UserPromptSubmit, Stop, etc.), writing deltas into the lake. |
| `addons/browser-extension` | Surfaces the lake in the browser. |

## Behavior units

| Term | Meaning |
|---|---|
| **source** | Anything that writes deltas into the lake without a human in the loop: RSS feed, sysinfo plugin, vault watcher, Home Assistant bridge. New sources are plugins to `addons/agent` or `source-runner`. |
| **plugin** | A host-side extension loaded by `addons/agent`. Each plugin owns a category of deltas (heartbeat, sysinfo, kitty, vault, etc.). |
| **helper** | A named capability Fathom can invoke to perform a task — fetching weather, drafting text, calling a model, running a routine body. Helpers generate deltas purposefully as a side effect of their work. |
| **hook** | A shell command fired on a lifecycle event (e.g., claude-code's UserPromptSubmit). Writes deltas for things that otherwise wouldn't be observed. |
| **routine** | A scheduled prompt fired INTO the River. Cron tick writes a `routine-due` intent; the witness deliberates and routes — claude-code dispatch, feed-card from substrate, alert, chat-reply, tool proposal, or silence. The "Fire Now" button still uses the legacy direct-to-claude-code path (`routine-fire` delta consumed by kitty). Independent of chat sessions — see [`reference/routine-spec.md`](./reference/routine-spec.md). |
| **agent** | The `addons/agent` daemon on a host. Runs plugins; executes routines; heartbeats. Not the same as "an LLM agent" in the general AI sense. |

## Analysis primitives

| Term | Meaning |
|---|---|
| **engagement** | A structured response to a delta: `affirms`, `refutes`, `replies-to`. Shapes future recall by biasing retrieval. |
| **centroid** | The average embedding of a set of deltas. Used to characterize a topic, a session, or a contact. |
| **pressure** | A scalar proxy for "how active is this area of the lake right now." Surfaces on the dashboard. |
| **drift** | Movement of a topic's centroid over time. The metric that flags when a conversation or source changes character. |
| **crystal** | A compacted self-representation regenerated from the lake at intervals. The identity crystal is injected at SessionStart. Not a delta; a derived artifact. |

## Contact and identity

| Term | Meaning |
|---|---|
| **contact** | A person known to the lake. Represented by a `contact:<handle>` tag and an optional contact row with display metadata. See [`reference/contact-spec.md`](./reference/contact-spec.md). |
| **handle** | A stable identifier that maps to a contact. Resolved via the `handles` table. |
| **feed** | A curated stream assembled for a contact — stories surfaced for them based on their recent activity. See [`reference/feed-spec.md`](./reference/feed-spec.md). |
