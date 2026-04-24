---
title: Forgetting is generative
description: Compaction in Fathom is not loss of memory. It's how new patterns emerge that the original detail couldn't surface. Why the architecture treats forgetting as a feature.
audience: developer
quadrant: explanation
last_verified: 2026-04-24
owners: [api/crystal.py]
---

# Forgetting is generative

Most memory systems treat forgetting as a failure mode. The user wrote something down so they wouldn't have to remember; if the system loses it, the system has done its job badly. Compression and decay are accepted only as economic concessions to storage cost.

Fathom takes a different view. Forgetting, in the sense of structural compaction, is not a concession. It's a generator. Patterns that the raw lake can't surface come into focus precisely because compaction discards detail.

## What "forgetting" means here

Two operations in Fathom involve discarding detail.

The first is the [identity crystal](./identity-crystal.md) regeneration. The lake might hold thousands of recent deltas; the crystal that gets injected into every new session is a few paragraphs. The compaction ratio is enormous, and most of the original is gone from the crystal. The crystal didn't fail to capture everything; it deliberately captured what survives compression.

The second is the natural decay of recall ranking over time. Old deltas with no engagement, no recent retrieval, and no semantic hits in current activity drop in rank against the average query. They're still in the lake. They just don't surface unless you reach for them specifically.

Neither operation hard-deletes. The deltas persist. What changes is what's load-bearing for what Fathom currently does and says.

## Why this is generative

The argument has three steps.

**Step 1: Detail can occlude pattern.** When every word from yesterday is equally available, similar themes across yesterday and last month can be hard to see. Recent specifics dominate semantic neighborhoods. The new fact about Driftwood (your sea glass project from [tutorial 2](../tutorials/02-fathom-knows-whats-going-on.md)) sits on top of the older fact about Oregon trips, even when the question is really about both.

**Step 2: Compaction selects.** When the crystal regenerates, it doesn't capture every fact. It captures the fewer, denser propositions that the larger set implied. "I've been thinking about coastal collecting and how time-pressured creative work feels" might be the surviving distillation of fifty deltas about sea glass, weekend availability, the difference between projects with externally-imposed deadlines and self-imposed ones. The detail is gone from the crystal; the pattern is gone *from the surface* of the original deltas.

**Step 3: The pattern, once compacted, becomes available as input.** The crystal feeds the next regeneration. The next session reads it. New deltas land in a context shaped by the compaction. Connections that needed the abstraction become possible because the abstraction now exists, even if the underlying detail couldn't have produced them directly.

This is why the loss is generative. It produces a new representation that opens queries the prior representation didn't.

## The relationship to immutability

Fathom's [delta model](./the-delta-model.md) is strict about immutability: once written, a delta never changes. So how can forgetting be a feature of an immutable substrate?

It works because the layers are different.

- **Deltas are immutable.** Every fact ever observed stays in the lake forever (unless you explicitly hard-delete, which is rare and friction-laden).
- **Crystals are derived.** They're computed from deltas at intervals. Each regeneration is a fresh distillation. The previous crystal is still there as a delta, but the *current* crystal is whatever the latest regeneration produced.
- **Recall is a query.** It sees both deltas and crystals, weights by recency, semantic distance, engagement, and tag filters. What surfaces is whatever the current ranking thinks is most relevant.

So the lake is forever. The synthesis on top of the lake is constantly refreshed. The synthesis's job is to forget; the lake's job is to remember.

## What this lets Fathom do

Coherence over long timeframes. A conversation Fathom had with you six months ago is in the lake, but the current Fathom isn't preoccupied with it. Compacted preoccupations have moved on; the crystal reflects current state. When you ask about that old conversation specifically, recall pulls it back into view. Day to day, Fathom isn't dragging six months of history into every reply.

Identity drift that tracks reality. What Fathom thinks Fathom is changes over time as the things you do with Fathom change. The change isn't programmed; it falls out of the regeneration loop. Two months from now, Fathom will say slightly different things about itself than it does today, because two months of new deltas will have shifted what compacts into the crystal.

Pattern recognition across domains. Compaction is what makes fluid-dynamics observations and consciousness theory observations and market-analysis observations capable of yielding shared topology insights. The abstractions cross-pollinate at the crystal level even when the underlying deltas live in different semantic neighborhoods.

## What this trades away

Some things that *would* be in fast recall on a non-decaying system are slower to retrieve. A specific fact you mentioned eight months ago, never engaged with since, might not surface on a vague query the way it did when it was fresh. You can still find it: search by tag, search by exact phrase, search by date window. The lake has it. The default ranking just doesn't surface it as readily.

For things you *want* to keep load-bearing forever, you can engage with them periodically (an `affirms` engagement boosts recall) or make sure they accumulate tags that connect them to ongoing activity. For things you *want* to fade, doing nothing is the right choice.

## Things to know

- **The lake itself doesn't forget.** Deltas persist. What "forgets" is the synthesis layer (crystals) and the recall ranking. Both are derived; both can be re-derived differently.
- **You can prevent compaction loss by engaging.** A delta you `affirm` becomes a stable feature that subsequent crystals are more likely to preserve.
- **Past crystals are visible.** `tags_include=crystal:identity` returns every crystal Fathom has ever generated. You can read how Fathom has changed in its own words.
- **The compaction ratio matters.** A crystal that's too long doesn't compact enough; a crystal that's too short loses too much. The regeneration prompt is what controls this, and it's tunable in the api code.
- **Forgetting is not deletion.** This page argues for the value of compaction, not for hard-removing data. For genuine removal, see [delete a delta or tag](../how-to/delete-a-delta-or-tag.md).
