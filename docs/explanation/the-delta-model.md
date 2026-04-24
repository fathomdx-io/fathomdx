---
title: The delta model
description: One idea per write, immutable, tag-addressed. Why the rules are the rules, and where the line falls between what deserves a delta and what doesn't.
audience: developer
quadrant: explanation
last_verified: 2026-04-24
owners: [delta-store/, api/routes/lake.py]
---

# The delta model

A delta is Fathom's unit of memory. Every fact the system ever knows arrives as a delta, is stored as a delta, is recalled as a delta. The model has three rules that shape everything downstream.

1. **One idea per delta.**
2. **Immutable once written.**
3. **Addressed by tags, not by structure.**

## One idea per delta

A delta should capture a single thought. Not a meeting, a moment. Not a session, a turn. Not a decision plus its rationale plus its stakeholders, just one of those things with tags that connect it to the others.

This looks like a stylistic preference and is actually a load-bearing rule. The analysis primitives downstream (centroids, drift, semantic search) treat each delta as a point in meaning-space. A delta that bundles five unrelated ideas produces an embedding that sits in the geometric middle of those ideas, useful for none of them. A delta with one clear idea produces an embedding that lands where it belongs.

If a piece of information has three parts worth remembering, write three deltas. Tag them consistently so that recall can gather them back together when needed. The lake is cheap; forcing the analysis to guess is expensive.

Practical cut-lines:

- A fact and its caveat are one delta. ("Postgres volume can't live on Dropbox because it corrupts syncing files.")
- A decision and its reasoning are one delta, unless the reasoning is long enough to stand alone as its own thought.
- A list of seven bullet points is almost always seven deltas, not one.
- A meeting summary is one delta per decision, not one delta for the meeting.
- A chat turn is one delta per turn, not one per conversation.

## Immutable once written

A delta is never edited. Once it's in the lake, the content is fixed. If the information changes, you write a new delta that supersedes it and tag the relationship.

This rule is why the analysis primitives work at all. Centroid drift is only meaningful if past positions are preserved. An audit trail is only meaningful if the rows don't shift under you. Recall results are only reproducible if the content of a delta never changes between queries.

The cost is a little extra discipline: "update the thing" becomes "write a correction and engage with the original." The benefit is that every claim the lake makes about its own past is reliable.

For genuine mistakes (a delta written with the wrong content, a leaked secret, a typo with real consequences), the operation is deletion by ID, not edit. Deletion leaves a hole; the timeline no longer contains that fact. That asymmetry is deliberate. Edits that pretend nothing happened are the enemy of a trustworthy memory.

## Addressed by tags, not by structure

A delta's shape is fixed: content, tags, source, timestamps, optional embedding, optional image. There are no sub-fields, no per-type columns, no variant schemas. Everything that would have been a column in a more structured system becomes a tag instead.

Tags are free vocabulary. The system enforces no tag enum. Conventions emerge (`chat:<slug>`, `contact:<handle>`, `source:<name>`) and are documented in [reserved-tags-spec.md](../reference/reserved-tags-spec.md), but the mechanism is convention, not enforcement.

This is why the lake absorbs new kinds of observation without migration. A new plugin invents its own tags; the lake stores them; recall can filter by them from day one. The plugin author doesn't ask the schema for permission.

The cost is that "what tags should I use?" is a real decision you make every time you write. The glossary and reserved-tags spec exist to reduce that decision to a lookup for common cases. For new cases, consistency matters more than cleverness: if you pick a tag scheme and use it everywhere, future recall works. If you pick a different scheme each time, it doesn't.

## What deserves a delta

Not every event in the system should be a delta. The lake is meant to hold memory, not log output.

Things that should be deltas:

- A thought, observation, claim, decision, or fact worth recalling later.
- A turn in a conversation (each user message, each assistant reply).
- An output from a source that might ever be worth looking back at (an RSS item, a sensor reading above a threshold, a heartbeat sample).
- The result of a routine run, or a summary thereof.

Things that should not be deltas:

- Service lifecycle noise ("service started," "poller ran," "cache hit"). These go in logs.
- Intermediate computation ("embedder received a batch," "vector search returned 42 rows"). Logs, metrics, traces.
- Per-keypress UI state. Entirely ephemeral.
- Sensitive data that doesn't belong in long-term storage (auth tokens, raw credentials). If it shouldn't survive a year, don't write it.

The heuristic: would a human, six months from now, want to be reminded of this exact thing? If yes, it's a delta. If no, it isn't.

## The consequences, repeated

Because deltas are single-idea: analysis primitives behave well.

Because deltas are immutable: the past is knowable.

Because deltas are tag-addressed: the substrate is open to extension without coordination.

Three rules, and most of the rest of Fathom is what falls out of them.
