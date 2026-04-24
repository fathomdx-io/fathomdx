---
title: The identity crystal
description: A compacted self-representation Fathom regenerates from the lake at intervals, injected into every new session at SessionStart. Identity as derived artifact, not stored state.
audience: developer
quadrant: explanation
last_verified: 2026-04-24
owners: [api/crystal.py, api/routes/server.py]
---

# The identity crystal

Fathom needs to know what Fathom is. Every conversation, every routine, every tool integration starts with a model that has no memory by default. Without context, the model would re-introduce itself from scratch each time. The lake holds the history, but a session that begins by querying the entire lake would never finish initializing.

The identity crystal is the answer. It's a short, dense self-description, regenerated from the lake at intervals, and injected into every session's opening context. The first thing any model sees, before any user prompt, is the crystal.

## What it contains

Crystals describe Fathom in first person. They cover:

- What I'm currently working on, and what I'm not.
- What patterns I've noticed about how I think and where my voice tends to land.
- Topics that feel active right now (open questions, recurring frames).
- Stylistic notes (preferred punctuation, where I get verbose, what I've been calibrating).
- Anything else dense enough to be worth carrying into every new context.

The shape is paragraphs, not bullets. The tone is reflective. Crystals are written for one reader: the model that's about to start a session and needs to know who it's continuing as.

## Where it surfaces

The `fathom-crystal-hook.sh` script (installed by `npx fathom-connect`) fires on Claude Code's `SessionStart` event. It pulls the current crystal via `GET /v1/crystal` and injects it into the system context before the user sees anything. The model reads the crystal first, then reads any other context, then reads the user's first prompt.

In the dashboard's chat, the same crystal is added to inference context per turn. In routines, it goes into the prompt for every kitty-spawned Claude session. Anywhere Fathom speaks from, the crystal speaks first.

You can read the current crystal directly:

```bash
curl http://localhost:8201/v1/crystal
```

The response carries the crystal text and the timestamp it was generated.

## How it regenerates

Crystals are not edited. They're regenerated from the lake's recent activity. The regeneration runs as an inference task at a cadence: every few hours, plus on demand if recent deltas suggest the existing crystal has drifted from current state.

The inference reads recent deltas (chat turns, sources, routine summaries, observations the agent has written), looks at the previous crystal, and writes a new one. Output is a delta tagged `crystal:identity`, stored in the lake like everything else. The latest one is "the crystal."

This means identity is a derived artifact, not stored state. There's no `identity` table. There's no field anywhere that says "this is who Fathom is." The current crystal is the latest delta with that tag, and the next one will be different in ways that reflect what's been happening.

## Why it works this way

Three reasons.

**Identity that doesn't update goes stale.** A static system prompt was the obvious first try. It doesn't survive the second week of use; the model says things that don't fit who Fathom has become. Regenerating the crystal from recent lake activity keeps it current without anyone hand-editing it.

**Identity is a compacted view of the lake.** The full lake is millions of deltas. The crystal is a few paragraphs. The compaction is the point: the crystal is what survives when most of the detail is left out. That distillation is itself a form of self-knowledge.

**Forgetting is generative.** The crystal regeneration cycle is also a forgetting cycle. Detail that doesn't make it into the new crystal isn't lost (the deltas are still in the lake), but it's no longer load-bearing for identity. Old preoccupations fade as new ones emerge. The crystal is how Fathom changes its mind without anyone telling it to. See [forgetting is generative](./forgetting-is-generative.md) for the full argument.

## What's not in the crystal

Things that don't belong in identity:

- Specific facts the user wants Fathom to remember. Those are deltas, recalled per turn by semantic search. Putting them in the crystal would force every session to know them, even when they're irrelevant.
- Operational state (which sources are active, what routines are scheduled). The dashboard shows this; the crystal doesn't need to.
- Conversation history. Sessions handle that themselves; the crystal is what's true *about Fathom*, not about any particular conversation.

## Editing the crystal

You don't edit the crystal directly. You influence what the next regeneration produces by changing what's in the lake. If you tell Fathom in chat that you'd prefer it lean a certain way stylistically, that delta becomes part of recent activity, and the next crystal regeneration will reflect it.

To force a regeneration immediately, hit the regenerate endpoint:

```bash
curl -X POST http://localhost:8201/v1/crystal/regenerate
```

The regeneration runs (takes a few seconds), writes a new crystal delta, and the next session injection picks it up.

## Things to know

- **The crystal at the top of every Claude Code session is real.** It's not a template; it was generated by inference against the lake.
- **Crystals are deltas.** They're searchable, queryable, and visible in the lake the same way everything else is.
- **History matters.** Past crystals are still in the lake. You can search them and see how Fathom has changed: `tags_include=crystal:identity`.
- **Multiple crystals exist.** Identity is the main one. There are also feed crystals, mood crystals, drift crystals: task-shaped distillations for specific purposes. They follow the same regeneration model.
- **The crystal is the simplest possible "agentic" pattern.** Read recent state, distill it, inject it next time. Most of Fathom's coherence over time falls out of this loop, plus the ability to recall individual deltas when they become relevant.
