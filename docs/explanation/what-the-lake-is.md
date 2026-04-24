---
title: What the lake is
description: Why Fathom stores everything in one pgvector-backed substrate instead of a tree of tables, and what that buys you.
audience: developer
quadrant: explanation
last_verified: 2026-04-24
owners: [delta-store/, api/routes/lake.py]
---

# What the lake is

Fathom has one place where memory lives. We call it the lake.

Concretely, the lake is a PostgreSQL database with the pgvector extension. A single table holds every unit of memory Fathom has ever seen: what you typed into chat, what an RSS feed delivered overnight, what a heartbeat plugin reported, what Claude Code wrote in response to your prompt. There is no "chat table" and a separate "feed table" and a separate "sensor table." There is one table, and every row is a delta.

That sameness is the whole point.

## The argument for a single substrate

Most memory systems start by carving the problem into types. Notes go here, emails go there, calendar events in a third place. Each store gets its own schema, its own query language, its own search. Over time the walls harden. Cross-domain search becomes impossible. You can find "emails about fluid dynamics" or "notes about fluid dynamics" but not "everything about fluid dynamics, ranked by recency."

The lake collapses that. A delta is a delta whether it came from a keyboard, an RSS poller, or a Claude Code hook. Every one has the same shape: content, tags, source, timestamps, an optional embedding, an optional image. That shape is flexible enough to hold any kind of observation and strict enough that one query can reach across all of them.

When you search for "fluid dynamics," you don't search four stores and merge the results. You search the lake.

## What a delta carries

Fields on every row:

- `content`: the text of the observation. Free-form. May be a sentence, a paragraph, a URL, a quote, a shell command, anything.
- `tags`: an array of strings. Tags are free vocabulary, not a closed enum. A few are conventional (`chat:<slug>`, `participant:user`, `contact:<handle>`, `source:<name>`) but any string is legal.
- `source`: who wrote this delta. `claude-code`, `fathom-agent`, `rss`, `homeassistant`, `user`, whatever.
- `created_at`: when the delta came into existence.
- `embedding`: vector representation of `content`, computed by the configured embedder. Nullable until computed; once set it drives semantic search.
- `image_path`: optional. If the delta is an image moment, the path points into the lake's image directory.
- `metadata`: a small JSON blob for per-source fields that don't deserve to be tags.

That's the whole model. No joins, no relations, no foreign keys pointing across schemas. Relation in the lake is expressed by shared tags, not shared rows.

## Why tags instead of foreign keys

Foreign keys force you to decide relationships at write time. They demand that the schema know, in advance, that a chat message belongs to a conversation, a conversation belongs to a user, a user belongs to a workspace. Each of those relationships is a row in another table, and every new kind of relationship is a schema change.

Tags invert that. A delta that tags `chat:fluid-ideas`, `contact:myra`, and `topic:navier-stokes` participates in three "relations" simultaneously, and the lake didn't have to be told in advance that those relations would exist. A year later, when you add a new concept (say, `mood:curious`), old deltas can be backfilled with the tag and immediately enter the new query. No migration, no schema change.

Tags are also how sessions work. A chat session isn't a row in a `sessions` table. It's a tag (`chat:<slug>`), and "the session" is the timestream of every delta that carries that tag. See [why a session is a tag](./why-a-session-is-a-tag.md) for the full argument.

## What lives in the lake, physically

The lake has two on-disk locations:

- **Postgres data** lives in a named Docker volume called `${COMPOSE_PROJECT_NAME}-pg` (default: `fathom-pg`). Managed by Docker or Podman. Cannot live on Dropbox or any other syncing filesystem because Postgres corrupts when pages are written non-atomically.
- **Everything else** (images referenced by image moments, backups, drift history, mood state, API tokens) lives under `LAKE_DIR`, which defaults to `~/.fathom/mind/`.

Neither path is inside the fathomdx checkout. Deleting the repo, renaming it, or cloning it somewhere else does not touch your memory. This is deliberate: the code is one thing, the lake is another, and the lake outlives any particular version of the code.

## What the lake is not

The lake is not a log. Logs are append-only records of *what happened*. The lake is append-only records of *what was observed*, which is a superset. A log entry and a log-about-a-log entry are both deltas, indistinguishably.

The lake is not a knowledge graph. Knowledge graphs impose ontology at write time: subject, predicate, object, each drawn from a fixed schema. The lake imposes almost nothing, and meaning emerges from tag conventions rather than declared structure.

The lake is not a vector database with some text attached. Vector search is one capability of the lake, not its organizing principle. Tag filters, time windows, source filters, and engagement-weighted ranking all compose alongside semantic similarity.

## Consequences

Because everything is deltas in one lake:

- **Convergence is free.** Adding a new source (a new RSS feed, a new sensor plugin, a new hook) does not require teaching the rest of the system anything. The new deltas show up in the same queries that everything else shows up in.
- **Analysis primitives compose.** [Centroid](../_glossary.md), [drift](../_glossary.md), [pressure](../_glossary.md), and [engagement](../_glossary.md) are defined over deltas in general. They work on any slice: a tag, a session, a contact, a time window, a semantic neighborhood.
- **Export is trivial.** The lake is a Postgres table plus a directory of images. `pg_dump` and `tar` are sufficient. No service-specific export path, no proprietary format.
- **Forgetting is a first-class operation.** Because every unit of memory is addressable and shaped identically, tombstoning a delta or scoping a tag to expire works uniformly. The identity crystal's observation that [forgetting is generative](./forgetting-is-generative.md) depends on this uniformity.

The single-substrate choice is what makes Fathom feel coherent rather than federated. Every other design decision in the system rests on it.
