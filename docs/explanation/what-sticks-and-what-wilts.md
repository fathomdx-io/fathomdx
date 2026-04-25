---
title: What sticks and what wilts
description: Why some deltas live forever and others die on a clock. The three-law model that decides what becomes memory.
audience: developer
quadrant: explanation
last_verified: 2026-04-25
owners: [delta-store/, addons/agent/plugins/, source-runner/sources/]
---

# What sticks and what wilts

Every row in the lake has an `expires_at` column. Most leave it null and live forever. Some set it explicitly and wilt on a deadline, swept by a reaper that runs every five minutes. Which deltas get which treatment is not a per-source operations decision. It's a structural one, and it sits underneath the whole memory system.

This doc is the contract.

## The three laws

**1. Authored sticks.**

What the user typed into chat. What Fathom said back. What got synthesized — sediment, identity crystals, mood, feed cards, engagements. Routine specs, routine fires, routine summaries. What the user intentionally captured by writing to a vault. None of these set `expires_at`. They live until the lake itself moves.

**2. Observed wilts.**

What was scraped, polled, or watched without intent. RSS items the system pulled overnight. Mastodon timeline the system pulled. Home Assistant sensor states. Heartbeats. Sysinfo. Browser captures. All of these set `expires_at`, and the reaper hard-deletes them when the clock runs out — most defaulting to thirty days, with denser low-signal streams (heartbeats, sysinfo) running shorter, and the user free to tune any source longer or shorter from the dashboard.

**3. Engagement is how observation becomes authored.**

When something the user observed turns out to matter, the act of saying so is itself a delta — `affirms:<id>`, `refutes:<id>`, `reply-to:<id>`. That engagement delta is authored memory by law (1), so it sticks. The original observation can wilt freely on its TTL; what mattered is preserved by the engagement delta that points to it and carries enough of its content to remain useful after the original is gone.

This third law is what makes (1) and (2) safe to be strict about. Without it, "observed wilts" would mean every interesting tweet, every meaningful sensor reading, every paper found through RSS would silently evaporate after thirty days. With it, the lake gets the user's signal: anything the user marked is preserved by the marking itself.

## Why these laws

Two opposite failure modes haunt every memory system.

A lake that wilts everything is a working buffer, not memory. Conversations evaporate; the system has no continuity. Recall pulls back nothing useful from last quarter because last quarter is gone.

A lake that keeps everything is a hoarder's basement. Sensor data outweighs every conversation by a million-to-one within a year. Search collapses under the weight of irrelevant detail. The signal a user actually authored — what they said, what Fathom said, what they decided — drowns in telemetry.

The cut-line of authored-vs-observed is the one cut that survives both failure modes. Memory is what the user (or Fathom, on the user's behalf) generated. Buffer is what the world poured in. The lake holds both, but only one of them survives the reaper.

That cut-line happens to also be the moral one. The user owns what they authored. The world owns what it broadcast. Forgetting a tweet is fine; forgetting a conversation is a betrayal. The TTL policy reflects whose voice is being preserved.

## The vault carve-out

Vault notes live behind a "source"-category plugin, but they don't wilt. This is the exception that proves the rule.

The vault watcher is a transport, not an observer. Files in the vault are something the user wrote on purpose, in their own time, intending for the words to mean something later. They're authored — the plugin just imports them into the lake without re-deciding their nature. Same logic for any future plugin whose role is "carry user-authored content into the lake": no TTL by default, because the content arrived already authored.

The test isn't "what category did the plugin get tagged with" — it's "did a human (or Fathom) author this thing as a thought, or did a poll loop notice it from outside." The first sticks. The second wilts. A plugin that watches a directory of intentional notes is in the first category even though its mechanics — periodic, file-system-driven, not user-typed-into-chat — look like the second.

When in doubt, ask: if the user vanished, would this delta still be a true thing they authored? Vault notes yes, sensor readings no.

## Engagement and append-only

The third law has a structural consequence. Without it, the natural way to "save" an observation would be to clear its `expires_at`. That mutation would be the first place anything in Fathom rewrites a delta. The lake has been append-only by construction — every delta is immutable once written, every operation produces new rows, never updates them. Mutating `expires_at` would punch a hole in that invariant for the convenience of one feature.

The engagement-as-authoring law removes the temptation. We don't need to mutate the original to preserve what mattered. We write a new delta — the engagement — that points at the target, carries enough of the target's content to stand on its own after the original wilts, and is itself authored memory. The append-only invariant holds. The original is allowed to die. What mattered is preserved by being re-authored, in the user's voice, on the user's timeline.

The engagement delta's shape:

- `tags`: `affirms:<id>` / `refutes:<id>` / `reply-to:<id>`, plus any contextual tags the engager wants.
- `content`: the engager's reason, alongside enough of the target's content to remain useful after the target reaps.
- `media_hash`: copied over from the target if it had one, so an affirmed image stays viewable after the source delta is gone.
- `source`: `fathom-engagement`.

The semantic interpretation:

- `affirms` — this mattered; remember it.
- `refutes` — this is wrong; remember why I disagreed.
- `reply-to` — this prompted a thought; remember the thought.

All three preserve. None mutate.

## Current defaults

The single source of truth for current TTLs by surface. Code comments should reference this table rather than duplicate values.

| Surface | TTL | Why |
|---|---|---|
| Chat user/assistant messages | forever | authored |
| Sediment, crystals, mood, feed cards | forever | synthesized memory |
| Vault notes | forever | authored (intentional save), transport plugin |
| Engagements (`affirms`/`refutes`/`reply-to`) | forever | how observation becomes authored |
| Routine spec / fire / summary | forever | authored by user or by routine; summary is the artifact |
| Contact proposals | 30d | open-ended workflow window |
| Resolved contact proposals | 90d | post-resolution audit window |
| Routine proposal (chat-event) | 6h | give the user time to confirm |
| Crystal-reject (debug breadcrumb) | 7d | not memory; forensic only |
| Chat-event scaffolding (tool use, silence ack) | 5min | UI noise |
| RSS / Mastodon / generic API source | 30d default | observed; user-tunable in dashboard |
| Browser extension capture | 7d default | observed; user-tunable, "never" toggle |
| Home Assistant state changes | 30d default | observed |
| Kitty fire receipts | 30d default | accountability breadcrumbs |
| Sysinfo telemetry | 1d default | observed, dense, low signal |
| Heartbeats | 24h | aliveness signal |
| Pair codes | 10min | ephemeral admin secret, not in lake |

The reaper sweeps every five minutes. Deltas with `expires_at <= NOW()` are hard-deleted. There is no soft-delete tier; once the reaper runs, the row is gone.

## For plugin authors

If your plugin's `category` is `"source"`, you're an observer. Set `expires_at` on every delta you push, with a configurable default. Surface the knob via `CONFIG_SHAPE.expiry_days` so the local-UI editor and the dashboard can render it. Thirty days is the conventional default unless you have a specific reason for shorter (sysinfo runs at a sample-per-five-minutes density and would drown the lake at 30d, so it defaults to 1d).

If your plugin's `category` is `"system"` or `"runtime"` (heartbeat, kitty, local-ui), ask whether the deltas you write are accountability artifacts (heartbeats, fire receipts — wilt on a clock) or authored work (routine summaries — stick). The category is a hint, not a rule; what matters is the nature of each delta's content.

If your plugin is a transport for user-authored content (vault, future Obsidian, future Roam, etc.), don't set `expires_at`. The content arrived already authored.

The exception you should not invent: do not mutate `expires_at` on an existing delta to "save" it. The lake is append-only. If a thing matters, write a new delta about it. That's what engagement is for.

## Cross-references

- [Forgetting is generative](./forgetting-is-generative.md) — the complementary case: compaction-by-recall-decay and crystal regeneration. Those operations don't hard-delete; this doc covers the operations that do.
- [The delta model](./the-delta-model.md) — one idea per delta, immutable, tag-addressed. TTL is layered on top of that immutability without breaking it (see "Engagement and append-only" above).
- [What the lake is](./what-the-lake-is.md) — why one substrate, why pgvector, why a single shape for everything.
- [Reserved tags spec](../reference/reserved-tags-spec.md) — engagement tag shapes (`affirms:<id>` etc.) and the rest of the reserved-tag conventions.
