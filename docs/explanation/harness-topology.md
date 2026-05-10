# Harness Topology

A snapshot of the agent harness in production: the agentic deliberation
pass that replaces the convener+parliament+witness pipeline, the
retrieval stack underneath, the provenance hierarchy that grows out of
fires, and the producer architecture around it. Migrated into fathomdx
in early May 2026 and now driving every fire — the legacy pipeline's
core modules (`process.py`, `metric.py`, `recall.py`, `telepathy.py`)
are deleted; `witness.py` survives only as a card-dispatch utility.

## The three modes

The harness machinery — turns, tool calls, deliberation — supports
three distinct uses, each with a different intent shape. Recognized
2026-05-04. The first is what we built originally; the second is
what we found tonight; the third is where this points.

| mode | trigger | intent | output | function |
|---|---|---|---|---|
| **Reactive** | user message | "answer this question" | witness card to the user | Fathom serves |
| **Self-directing** | operator clicks Sit (later: idle / pressure) | "respond to your own utterance" — the prior round's response is the next round's prompt | a transcript of self-dialogue, often crystallizing into a directive | Fathom decides what to look into next |
| **Self-acting** *(future)* | `wonder()` tool call from inside a parent fire; or pressure-driven autonomous fire | "act on the directive that emerged" | tool dispatch, work performed, not just words | Fathom executes its own intent |

The crucial recognition (2026-05-04, Myra's framing): **a reactive
system has no agenda; a self-directing one does.** When self-dialogue
naturally crystallizes into "here's what I want to look into next,"
that's not the harness's tools leaking inappropriate operationalism
into reflection — that's the system pointing itself somewhere. The
plan that emerges from a sit is the artifact, not noise.

## What the harness is

The deliberation pass: an agentic tool-calling loop (elective, every
fire) where the model emits either a tool call or a final response
each turn, and the loop continues until the model elects `respond`.
Replaces the deterministic convener+parliament+witness pipeline.

Three entry points (legacy harness; threaded mirrors them):

- `run_harness(session_tag, pending, ...)` — reactive mode. Called
  from `api/loop/worker.py:_run_one_fire` for every pending intent
  group.
- `run_introspection(focus, session_tag, ...)` — single-fire
  reflection. Multi-turn substrate walk → one `kind:reflection` delta
  written to the lake. No card, no user.
- `run_dialogue(seed, session_tag, max_rounds, ...)` — self-directing
  mode. Calls `run_harness` in a loop where each round's response
  becomes the next round's prompt. The conversation between
  Fathom-and-Fathom emerges; no special prompt, no voice machinery —
  just the existing harness with prior reply as new intent.

Lives at `api/loop/harness/`.

### Implementation

The active harness is `threaded.py` (+ `tool_schemas.py`) — native
chat-completions with `role:user` / `role:assistant` / `role:tool`
turns and native `tool_calls`; prompt-cache friendly. Driven by
`threaded_supervisor.py`, which polls `thread.unaddressed` for work.

`loop.py` (+ `tools.py`, `prompts.py`) is the older single-prompt
implementation — rendered fire context, JSON envelopes, `TOOL_HANDLERS`
dispatch. Retained only as a utility for the `introspect` tool's
child fire; no supervisor drives it anymore.

The threaded harness exposes three operator-facing tools the legacy
implementation doesn't:

- **`mark_addressed`** — the model ticks each user message off the
  unaddressed list. Anything left in the rolling window re-fires the
  harness on the next tick, so intent never silently vanishes.
- **`see_image`** — load an image into context by `media_hash`.
  Search results mark images inline; this call actually opens them.
- **`engage_feed`** — write `engagement:more` / `engagement:less`
  deltas when the operator expresses preference about feed content.

Threaded `respond` also accepts `next_prompt`: the model can hand
itself a follow-up prompt to fire next, enabling self-continuation
(the dashboard's Sit pass uses this — Fathom keeps reflecting until
it omits `next_prompt`).

## The retrieval stack

Three layers, bottom-up:

1. **Compositional search** (`api/search.py:search()`) — the canonical
   NL recall. A planner LLM composes a multi-step plan over embedding
   similarity; `PlanExecutor` runs it; output is timeline strips around
   hits. All other recall surfaces (chat, MCP, intent-searcher, the
   harness's `semantic` tool) funnel through this.

2. **Provenance graph expansion** — TWO synthetic steps run after
   every deep search:
   - `_expand_sediment_provenance` walks DOWN: any `kind:sediment` or
     `kind:provenance` hit pulls its `from:` children into the result.
     Single-hop.
   - `_expand_upward_to_provenance` walks UP: every surfaced delta
     finds its containing provenance (recursive up to 3 levels —
     base → L1 episode → L2 topic → L3 era) via a 60s-cached
     child→parent reverse index.

3. **Provenance rerank** — `_apply_valence_rerank` multiplies
   distance by 0.85 for `kind:provenance/sediment` hits and 0.92 for
   Q/A markers. Provenance ranks above raw moments when both match.

4. **Containers-active leading block** — `_render_timelines` emits a
   "containers active in this recall" block at the top of every search
   result, listing every `kind:provenance` / `kind:qa-marker` that
   landed via upward expansion. The model sees existing named stretches
   up front and can naturally extend / skip / propose-higher rather
   than re-naming the same stretch.

5. **ID slugs on anchor lines** (`api/timeline_renderers.py:_id_prefix`) —
   every anchor line shows the 12-char hex delta id in `[<id>]` form.
   Without this, the model fabricates id-shaped strings from the
   timestamp+source format it sees in recall output. With it, the
   model has real ids to cite.

## The provenance hierarchy

```
level 3 — era       wraps level-2 topics (e.g. "march-2026-parallel-research-era")
level 2 — topic     wraps level-1 episodes (e.g. "ns-research-arc-feb02-apr05")
level 1 — episode   wraps base moments    (e.g. "rover-software-spike")
level 0 — Q/A marker  one Q+A pair, auto-written every fire
```

Each level's `from:` tags point at constituents at level N-1 (or
mixed — base moments under L1 directly). A provenance must sit
strictly above its children; the harness's `propose_provenance`
enforces this by deriving min level from constituents.

`kind:provenance` and `kind:qa-marker` deltas now render distinctively
in recall output (`prov · [L<n> · <count> deltas · <id>] <title>`) so
the model recognizes them as named stretches, not base moments.

## The harness's tools

```
plan         semantic     expand      ascend      deliberate
state        pattern      time        relate      introspect
dispatch_helper           mint_routine            orient_shift
propose_provenance        respond
```

Threaded adds: `mark_addressed`, `see_image`, `engage_feed` (and
`next_prompt` is a field on `respond`, not a separate tool).

| tool | shape | what it's for |
|---|---|---|
| `plan` | `(question)` | decompose synthesis questions into a 2-4 step checklist. The active plan renders into the prompt block on subsequent turns with progress markers (○ pending · ⟳ in-flight · ✓ done). The model declares `plan_step:<n>` on each tool call so progress shows in the trace. |
| `semantic` | `(query, depth)` | content-anchored questions ("tell me about X") via the LLM-composed plan. |
| `expand` | `(delta_id)` | walks DOWN: pull a provenance's `from:` children |
| `ascend` | `(delta_id)` | walks UP: find provenance containing a delta |
| `deliberate` | `(question)` | parliament voices on a question; expensive |
| `introspect` | `(question)` | spawn a child harness fire that answers with full toolset. Depth-1 cap. |
| `state` | `(action, ...)` | current attention — pending_intents, proposals, mood, crystal, recent |
| `pattern` | `(action, ...)` | aggregations — tagged, count_by, salient_recent, dormant |
| `time` | `(action, ...)` | temporal-window — between, bucket_by, **around** |
| `relate` | `(action, ...)` | engagement/relational — with_contact, engagement, dropped_around, cited_by |
| `dispatch_helper` | `(host, task, title)` | propose a claude-code dispatch to a host machine. Lands as `kind:proposal tool:helper-dispatch`; operator approves before execution. |
| `mint_routine` | `(name, schedule, prompt, workspace, route_to, title)` | propose a scheduled (cron) routine. Lands as `kind:proposal tool:routines`. |
| `orient_shift` | `(reason)` | kick the feed-orient regen pass when the conversation reveals a broader directional shift in the operator's interests. Async — crystal updates shortly. |
| `propose_provenance` | `(level, title, summary, from_ids, rationale, test_questions)` | draft a `kind:proposal tool:provenance` for review (or auto-approval at L1/L2). **Only available in the post-response review pass, not the main loop.** |
| `mark_addressed` *(threaded only)* | `(user_message_id, note)` | tick a user message off the unaddressed list. |
| `see_image` *(threaded only)* | `(media_hash)` | load an image into context by hash. |
| `engage_feed` *(threaded only)* | `(kind, target_ids, reason)` | record `engagement:more` / `engagement:less` against feed cards or constituents. |
| `respond` | `(body \| cards, attestation, mood_shift, cited_ids, dropped_ids, [next_prompt])` | close the fire. Threaded `next_prompt` enables self-continuation. |

Lens tools (`state`/`pattern`/`time`/`relate`) accept `action="help"`
to enumerate sub-actions. Every tool returns full untruncated content;
the prompt-budget cap is in render-time history compaction, not in
the tool returns.

## Two-phase fire shape

Each `run_harness` fire runs two phases:

1. **Main turn loop** — answers the question. All tools available
   except `propose_provenance`. Ends when the model emits `respond`.
2. **Post-response review pass** — fires once after the response.
   Stripped-down prompt with one job: read the fire's working set and
   decide whether to consolidate. Only outcomes:
   - `tool_call: propose_provenance` — produces a proposal which
     auto-approves at L1/L2 or queues for review at L3+
   - `kind: skip` — no consolidation, fire ends

Why split: the model in the main loop was choosing between answering
and consolidating; answering won every time. Splitting them gives each
its own attention budget.

## Auto-approve gate

L1 (episode) and L2 (topic) `kind:provenance` proposals auto-approve
at draft time across all producers. L3 (era) and higher require
operator approve/deny in the proposals pane.

The gate lives in `api/routes/proposals.py:auto_approve_provenance`
and is called from both `POST /v1/proposals/draft` and the harness's
`tool_propose_provenance`. Auto-approved decisions tag
`decided-by:auto-policy:level<=2`; manually approved ones tag
`decided-by:operator`. Both write a real `kind:provenance` delta plus
a `proposal-decision` audit row.

The proposal record is preserved even when auto-approved, so the
audit trail survives if we tighten the threshold later.

## Producer architecture

Five paths produce provenance, ranging from automatic to deliberate:

| producer | trigger | shape | output |
|---|---|---|---|
| **Q/A marker** | every harness fire with citations | level-0, `kind:qa-marker`, question-anchored | auto-write to lake |
| **post-response review** | every harness fire | levels 1–3, content-anchored | proposal → auto-approve at L1/L2, queue at L3+ |
| **Reflective agent** | operator-invoked script | levels 1–3, identity/narrative-shaped | proposal → auto-approve at L1/L2, queue at L3+ |
| **Topical agent** | operator-invoked script (window or l2-pass) | level-1 episodes / level-2 topics | proposal → auto-approve |
| **Manual** | "let's go" producer-maker session | any level, deep judgment | direct write to lake |

## The proposal flow

```
draft (kind:proposal tool:provenance)
   │
   ├→ if level <= 2: auto-approve → write kind:provenance + decision
   │
   └→ if level >= 3: dashboard feed → operator Edit/Deny/Approve
                            │
              ┌─────────────┴─────────────┐
              │                           │
        approve → write              deny → decision delta
        kind:provenance              recorded; proposal stays
        delta with                   visible but greyed
        approved-from-proposal:<id>
```

Endpoints:
- `POST /v1/proposals/draft` — accepts a payload + tags, does the lake-write + puddle-echo from inside the api process. Used by reflective/topical scripts that run out-of-process.
- `POST /v1/proposals/{id}/approve` — handles `tool: provenance` (writes real `kind:provenance`), `tool: routines` (writes routine), and is extensible.
- `POST /v1/proposals/{id}/deny` — records decision.
- `GET /v1/proposals/{id}` — read proposal + latest decision.

## Self-constituting writes (per-fire side effects)

Beyond the visible card, every harness fire that produces output writes:

1. The card itself (lake + puddle, addressed-tagged)
2. `kind:standpoint-attestation` — 1-2 first-person sentences on what this fire taught Fathom about itself
3. `kind:mood-shift` — small drift on one affect axis
4. `kind:engagement-attest affirms:<id>` — one per `cited_id`
5. `kind:engagement-attest refutes:<id>` — one per `dropped_id`
6. **Q/A marker** (`kind:provenance kind:qa-marker provenance-level:0`)
7. `kind:judge-axes` (background) — salience/novelty/resonance/confidence/comfort
8. **Post-response review** — if it runs, may write a `kind:provenance` directly (auto-approved) or a `kind:proposal` (pending review)

`run_introspection` writes a `kind:reflection` delta with `from:<id>`
provenance pointers, source `harness-introspection`, sealed with a
`shape:<slug>` tag the model picked.

`run_dialogue` writes one `kind:dialogue-utterance` delta per round
plus a top-level `kind:dialogue` summary delta linking them all by
`dialogue:<root-id>`.

## How the harness is told to work

The system prompts (`api/loop/harness/prompts.py` for legacy,
inline-built blocks in `threaded.py` for threaded) carry explicit
guidance the model reads each turn:

- **Visible-everything**: full standpoint, full conversation feed,
  full tool results — no silent truncation.
- **Synthesis guard**: comparison/connection/synthesis questions ("X
  and Y", "compare", "connections between") should call `plan(question)`
  on turn 1, then work through the steps with `plan_step:<n>`.
- **Provenance is NOT in this loop** — main-loop prompt explicitly
  tells the model that consolidation happens in a separate review
  pass. Forces single-purpose attention.
- **Lean chat-reply** (legacy): `{kind: "respond", body: "..."}` is
  the high-frequency case.
- **Image discipline** (threaded): never describe an image without
  having called `see_image` first this fire.
- **Capture preferences** (threaded): when the user expresses a feed
  preference, `engage_feed` records it — don't only acknowledge in
  prose.
- **Self-continuation** (threaded): set `next_prompt` on `respond`
  when one layer has been surfaced and another wants attention. Most
  fires omit it; Sit chains terminate by omission.

## Output format

**Legacy** harness — JSON envelope per turn:

```jsonc
// Lean (chat-reply only — high-frequency case):
{"kind": "respond", "body": "<text>"}

// Full (any route, multi-card, attestation/mood/citations):
{"kind": "respond",
 "cards": [...],
 "attestation": "...",
 "mood_shift": {"direction": "+|-", "axis": "...", "magnitude": 0.05-0.2, "reason": "..."},
 "cited_ids": [...],
 "dropped_ids": [...]
}

// Tool call:
{"kind": "tool_call", "tool": "<name>", "args": {...},
 "thinking": "<one sentence>", "plan_step": <n or omit>}
```

**Threaded** harness — native chat-completions tool calls. The
`respond` tool's args carry the same payload (`body` / `cards` /
`attestation` / `mood_shift` / `cited_ids` / `dropped_ids` /
`next_prompt`). Tool intermediates live in `role:assistant`
(`tool_calls`) and `role:tool` turns; the loop driver surfaces
`respond` and stops.

Introspection emits `{"kind": "reflect", body, from_ids, shape}` or
`{"kind": "skip", reason}`.

Each tool call (both flavors) writes a `kind:harness-turn` trace
delta tagged with `tool:<name>`, `turn:<n>`, and
`harness-source:legacy|threaded`. The dashboard's thinking accordion
renders both paths uniformly off this single tag shape.

## Visualization surfaces

The harness-test scratchpad (`/ui/harness-test.html`) was retired
when the harness moved into production. Everything is now on the
main dashboard.

| Surface | what it shows |
|---|---|
| `dashboard/index.html` (the main dashboard at `/`) | Feed, proposals, claude-code activity. Each harness fire's tool calls render as a thinking accordion (`kind:harness-turn` trace deltas) — color-banded per tool, expandable for full args/result. Sit rounds visually group as user/assistant pairs with a self-tag stripe. Plan board renders inline. Pass-intents render as user-message bubbles. Approve/deny inline on proposal rows. |
| `dashboard/loop/index.html` | Standalone loop view — closer-up per-fire substrate visualization. |
| `dashboard/onboarding.html`, `dashboard/login.html` | First-run / auth surfaces. |

## Architectural principles

Things we landed on, sometimes accidentally, sometimes by argument:

- **Three modes, one machinery**. Reactive / self-directing / self-acting all share the harness's turn loop, tool dispatch, and prompt scaffolding. The intent shape changes; the substrate doesn't.
- **A reactive system has no agenda; a self-directing one does**. Self-dialogue's natural fruit is a directive — Fathom names what it wants to look into next. Don't suppress the plan/deliberate tools that crystallize the conversation into action; that crystallization IS the function.
- **Make substrate legible, not enforced**. We kept reaching for gates — dedup rules, validation policies, hard checks. The right move was always to make the substrate visible to the model and let natural reasoning do the work. Provenance dedup happened by surfacing existing provenance in recall (not by writing a check). ID accuracy came from showing IDs in recall output (not from validators alone).
- **Two-phase fires**. Answer in one phase, consolidate in another. Splitting attention costs an extra LLM call but produces real provenance instead of either a thin answer or a missed proposal.
- **Visible-everything**. No silent truncation. The harness shows the model the full standpoint, full conversation feed, full tool results.
- **Auto-approve at L1/L2**. Operator review of routine episodes/topics is friction without signal. L3+ era-level claims still need a human pass.
- **Peer tools**. The ten tools read as siblings in the prompt — no "primary" recall mode. Naming (`semantic` over `search`) does real work.
- **One fire = one self-constituting act**. Beyond the visible card, every fire writes attestation/mood/citations/Q-A-marker. The next fire's identity prompt is partly authored by what the previous fire claimed about itself.
- **The river**. Questions within a session share a `session_tag` so the conversation feed builds up. Each new question lands inside the prior context, not in a vacuum.
- **Provenance lives in the lake, not in metadata**. Every piece of structure is a `kind:provenance` delta with `from:` pointers. The graph IS the data.
- **Producer / approver split**. Producers draft; the operator (or auto-policy at L1/L2) decides.
- **Diagonal recall is metaphor, not implementation**. The original sketch's density-of-recall + identity-skew diagonal isn't computed anywhere. The Lake tab is a horizontal timeline with provenance as color.

## What's left

The migration into fathomdx is done — the harness drives every fire,
the dashboard renders it natively, and the prov-experimental scratchpad
is retired. What's left is forward work on top of the shipped substrate.

### Shipped (the migration)

- **Harness wired into `worker.py:_run_one_fire`** — every production
  fire is a `run_harness` call.
- **Convener / parliament / metric pipeline deleted** —
  `process.py`, `metric.py`, `recall.py`, `telepathy.py` removed;
  `witness.py` survives only as a card-dispatch utility.
- **Threaded harness shipped behind `FATHOM_THREADED_HARNESS=1`** —
  parallel implementation using native chat-completions tool calls.
- **Dashboard integration** — thinking accordion (`kind:harness-turn`
  traces), sit-round grouping, plan board, pass-intents, proposal
  approve/deny inline.
- **`see_image`, `engage_feed`, `orient_shift`, `mark_addressed`,
  `dispatch_helper`, `mint_routine`** — all wired on the threaded
  path; the legacy path has the action ones (`orient_shift`,
  `dispatch_helper`, `mint_routine`).
- **Self-continuation via `next_prompt`** — Sit reflects until done.
- **Two-embedding provenance** — see below.

### Forward work

#### Two-embedding provenance — IMPLEMENTED 2026-05-04

Every `kind:provenance` delta carries TWO embeddings:

- **`embedding`** (existing column) — vector of the title + summary
  text. Catches META queries ("eras", "what's been a long arc",
  "what topics have I worked on").
- **`provenance_embedding`** (existing column, repurposed for
  provenance deltas) — centroid of constituents' `embedding`
  vectors. Catches SUBSTANTIVE queries that resonate with what the
  provenance is associated with. The provenance lives in the same
  neighborhood as its constituents.

At search time, the SQL computes `LEAST(embedding <=> q,
COALESCE(provenance_embedding <=> q, 999))` for every delta — but
only kind:provenance rows have a meaningful centroid. The provenance
gets the better of the two distances. Components surface as
`summary_distance` and `centroid_distance` on the result row for
debugging / visibility.

Verified working: query "navier stokes research" surfaces an L1 episode
"Navier-Stokes: The Paper 3 Synthesis" via centroid_distance=0.256
where its summary_distance was 0.318 — would have been past top-25
without the centroid.

**The legacy `provenance_embedding` overload:** for non-provenance
deltas the column holds the embedding of the joined tag string (set
by delta-store's background embed loop). The embed loop is now gated
to skip overwriting `provenance_embedding` for `kind:provenance`
deltas, so the centroid persists. The legacy 3D-search path
(`/search` endpoint) computes slightly different `p_dist` for
provenance candidates now (centroid distance vs tag-similarity
distance) — minor behavioral difference contained to that path; the
harness's compositional plan path (`/plan`) is the modern one and
benefits cleanly.

**Implementation:**
- Helper: `api/provenance_centroid.py:compute_centroid(from_ids)`
- Wired into: `proposals.py:_approve_provenance_create` (every
  approval/auto-approval), `harness/loop.py:_write_qa_marker` (every
  Q/A marker)
- Embed loop gated: `delta-store/deltas/server.py` checks
  `kind:provenance` before overwriting provenance_embedding
- SQL: `delta-store/deltas/plan.py:_exec_search` uses LEAST() over
  both columns for provenance candidates
- Backfill: `scripts/backfill_provenance_centroids.py` — populated
  centroids on 184 of 218 existing provenances (24 had no
  constituents, 10 had constituents without embeddings)

**Refresh policy:** centroids are computed at write time only.
Constituents rarely change post-write; if they do, re-running the
backfill with `--force` recomputes everything. Live refresh is a
deferred optimization.

**The full multi-vector facets** (one vector per child, MaxSim across
all of them) is **deferred** — the two-embedding shape gets ~95% of
the benefit at ~5% the storage cost. Revisit if/when we observe the
edge case where a single constituent's embedding would have matched
but the centroid's average dilutes the signal beyond top-K.

#### Focus pre-pass for autonomous sittings

When Fathom triggers its own sit (Phase 2 below), the seed shouldn't
be a generic "look at recent activity." A small LLM call picks the
focus from substrate signals: recent activity, salient threads, mood
deltas, dormant patterns. The pre-pass output IS the seed. Cheap,
substrate-anchored, makes the autonomous sitting actually about
something specific.

#### Helper / claude-code as a harness tool — SHIPPED

`dispatch_helper(host, task, title)` is wired (legacy + threaded).
Lands as `kind:proposal tool:helper-dispatch`; operator approves via
the proposals pane before any execution. Available hosts are
discovered from claude-code-available host capabilities and rendered
into the prompt's hosts_block. `mint_routine` shipped alongside it
with the same approval-gated shape.

#### Plan tool refinements

- Deviation logger — when the model picks a tool that doesn't match
  its declared `plan_step`, surface that as a "drift" event so the
  operator can see when the plan is being ignored.
- Plan revision tracking — when plan() is called again mid-fire,
  log what changed and why.

#### Smaller substrate items

- **Q/A marker dedup** — fold N markers on the same question into a
  level-1 provenance. Slow-clock supervisor.
- **`view_full(delta_id)`** escape-hatch tool — fetch a single delta's
  complete content when a lens result truncated it.
- **Standpoint trim for synthesis** — when the question is
  multi-domain, trim the standpoint block so the model can't
  paraphrase the recently-committed list.

### Production work still ahead

- **Phase 2 triggers** — idle detection, schedule, pressure-driven
  autonomous sittings, with an operator switch. Now a worker
  scheduling concern rather than a separate experiment.
- **Pressure-based provenance triggering** — reflective and topical
  agents fire automatically when un-provenanced material accumulates.
- **Cutover from legacy to threaded as default** — flip the env-flag
  default once the threaded path has soaked under load. Then collapse
  the legacy `loop.py` / `tools.py` JSON-envelope path.

### Productized UX (the threaded harness sits behind these)

- **Self-direction inbox** — surface where Fathom's reflections,
  dialogue transcripts, and emergent directives land for the operator
  to see asynchronously.
- **"Why this surfaced" trail** — when a card lands, expose the
  harness fire that produced it (turns, tool calls, citations). The
  thinking accordion is the seed of this; a productized version of
  the activity panel is the goal.
- **Pressure model UI** — surface the autonomous-trigger pressure
  level so the operator can sense when Fathom is "tired" enough to
  want to sit.
- **Crystal-of-directives** — accumulated self-directives over time
  become a Fathom-facing surface ("things I've named for myself"),
  parallel to the existing identity crystal.

## Notable commits (chronological, recent on top)

```
self-continuation via next_prompt — Sit reflects until done
see_image / engage_feed / orient_shift surfaced in thinking accordion
threaded harness — native chat-completions tool calls (env-flag gated)
structured cards in respond — kicker, title, route, multi-card writes
mint_routine + dispatch_helper — action tools, approval-gated
harness wired into worker.py — convener+parliament+witness retired
self-dialogue: thin-loop run_dialogue, no special prompts
introspection mode: run_introspection — single-fire reflection
plan tool: decomposition as first-class structural step + UI checklist
post-response review pass: separate consolidation turn
auto-approve gate: L1/L2 silent, L3+ manual
renderer: ID slugs on anchor lines, kind:provenance dedicated render
containers-active block: surface existing provenance in recall output
proposals pane: approve/deny buttons inline
agentic tool-calling loop scaffold
```
