# Harness Topology

A snapshot of the agent harness, its retrieval stack, the provenance
hierarchy, and the producer architecture as of `feat/agent-harness`.

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

A drop-in replacement for `witness.run_witness` that turns the
convener+parliament+witness pipeline (deterministic, every fire) into
an agentic tool-calling loop (elective, every fire). The model emits
a JSON envelope each turn — either a tool call or a final response —
and the loop continues until the model elects to respond.

Three entry points:

- `run_harness(session_tag, pending, ...)` — reactive mode. Same
  return shape as `run_witness`. Drop-in replacement.
- `run_introspection(focus, session_tag, ...)` — single-fire
  reflection. Multi-turn substrate walk → one `kind:reflection` delta
  written to the lake. No card, no user.
- `run_dialogue(seed, session_tag, max_rounds, ...)` — self-directing
  mode. Calls `run_harness` in a loop where each round's response
  becomes the next round's prompt. The conversation between
  Fathom-and-Fathom emerges; no special prompt, no voice machinery —
  just the existing harness with prior reply as new intent.

Lives at `api/loop/harness/`.

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
plan        semantic    expand    ascend    deliberate
state       pattern     time      relate    propose_provenance
```

| tool | shape | what it's for |
|---|---|---|
| `plan` | `(question)` | decompose synthesis questions into a 2-4 step checklist. The active plan renders into the prompt block on subsequent turns with progress markers (○ pending · ⟳ in-flight · ✓ done). The model declares `plan_step:<n>` on each tool call so progress shows in the trace. |
| `semantic` | `(query, depth)` | content-anchored questions ("tell me about X") via the LLM-composed plan. |
| `expand` | `(delta_id)` | walks DOWN: pull a provenance's `from:` children |
| `ascend` | `(delta_id)` | walks UP: find provenance containing a delta |
| `deliberate` | `(question)` | parliament voices on a question; expensive |
| `state` | `(action, ...)` | current attention — pending_intents, proposals, mood, crystal, recent |
| `pattern` | `(action, ...)` | aggregations — tagged, count_by, salient_recent, dormant |
| `time` | `(action, ...)` | temporal-window — between, bucket_by, **around** |
| `relate` | `(action, ...)` | engagement/relational — with_contact, engagement, dropped_around, cited_by |
| `propose_provenance` | `(level, title, summary, from_ids, rationale, test_questions)` | draft a `kind:proposal` for review (or auto-approval at L1/L2). **Only available in the post-response review pass, not the main loop.** |

Lens tools (`state`/`pattern`/`time`/`relate`) accept `action="help"`
to enumerate sub-actions. Every tool returns full untruncated content;
the prompt-budget cap is in `render_tool_history`, not in the tool
returns.

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

The system prompt (`api/loop/harness/prompts.py`) carries explicit
guidance the model reads each turn:

- **Visible-everything**: full standpoint, full conversation feed,
  full tool results — no silent truncation
- **Synthesis guard**: comparison/connection/synthesis questions ("X
  and Y", "compare", "connections between") should call `plan(question)`
  on turn 1, then work through the steps with `plan_step:<n>`.
- **Provenance is NOT in this loop** — main-loop prompt explicitly
  tells the model that consolidation happens in a separate review
  pass. Forces single-purpose attention.
- **Lean chat-reply**: `{kind: "respond", body: "..."}` is the
  high-frequency case.

## Output format

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
```

Tool calls:

```jsonc
{"kind": "tool_call", "tool": "<name>", "args": {...}, "thinking": "<one sentence>", "plan_step": <n or omit>}
```

Introspection emits `{"kind": "reflect", body, from_ids, shape}` or
`{"kind": "skip", reason}`.

## Visualization surfaces

| URL | what it shows |
|---|---|
| `/ui/harness-test.html` | Chat-shape harness page. Fire button runs reactive mode (user → Fathom). Sit button runs self-dialogue (Fathom ↔ Fathom in rounds). Each round renders as a normal user/assistant pair; the orange-stripe self-tag distinguishes self-utterances. Live stage labels in the "thinking…" placeholder. Plan board renders inline when `plan()` is called, with ○ ⟳ ✓ glyphs and live progress. Side panel polls every 4s for harness/reflective/topical proposals. Approve/deny buttons inline on pending rows. |
| `/ui/harness-test.html` Lake tab | Per-fire visualization. Horizontal timeline (BIRTH ← TIME → NOW). Resonant deltas above the line, colored by their containing provenance. Provenance bands below the line. Empty until a fire surfaces something. |
| `/ui/lake-topology.html` | Standalone analytical topology view of all provenance. Era / topic / episode / Q-A bands top-to-bottom, nodes positioned at constituent barycenters. |
| `/ui/lake-sketch.html` | Standalone pencil-on-paper rendering — mirrors the original notebook sketch. |
| `/ui/index.html` (dashboard) | Normal dashboard — feed includes proposals with Edit/Deny/Approve flow. |

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

### 1. Production wiring

- **Wire harness into `worker.py:_run_one_fire()`** — replace the convener+parliament+witness pipeline with a single `run_harness()` call.
- **Delete the old pipeline** — once the harness has been live for a stretch.

### 2. Phase 2 — triggering self-direction

The Sit button is operator-invoked. Self-dialogue should fire automatically when conditions warrant.

- **Idle detection** — no conversation activity for N hours, mood drift settling, no pressure crossing → trigger sit.
- **Schedule** — daily cadence, similar to existing routine fires.
- **Pressure-driven** — when un-sat-with material accumulates, the system itself raises the urge to reflect.
- **Switch** — operator can disable autonomous sittings.
- **Focus pre-pass** — when Fathom triggers its own sit, a small LLM call picks what to sit with from substrate signals (recent activity, salient threads, mood deltas) instead of using a generic seed.

### 3. Phase 3 — `wonder()` tool

A new tool in the regular harness that spawns a child introspection or dialogue mid-fire. The parent fire can cite the resulting reflection delta. This is the "self-acting" mode in miniature: a reactive fire decides it wants to sit with something, sits, and integrates the result.

### 4. Pressure-based provenance triggering

The reflective and topical agents currently run only when invoked. Should fire automatically when un-provenanced material accumulates.

### 5. Phase 2b — multi-vector provenance facets

Each provenance node carries the embeddings of its direct children as facets, so semantic search finds the parent via constituent content, not just summary content. Schema sketch only.

### Smaller items on the queue

- **Q/A marker dedup** — fold N markers on the same question into a level-1 provenance.
- **`view_full(delta_id)`** escape-hatch tool.
- **Standpoint trim for synthesis** — when the question is multi-domain, trim the standpoint block.
- **Pressure model UI** — surface the pressure level in the dashboard.
- **Deviation logger for plan tool** — when the model picks a tool that doesn't match its declared `plan_step`, surface that as a "drift" event.

## Notable commits (chronological, recent on top)

```
self-dialogue: thin-loop run_dialogue, no special prompts
introspection mode: run_introspection — single-fire reflection
plan tool: decomposition as first-class structural step + UI checklist
post-response review pass: separate consolidation turn
auto-approve gate: L1/L2 silent, L3+ manual
renderer: ID slugs on anchor lines, kind:provenance dedicated render
containers-active block: surface existing provenance in recall output
proposals pane: approve/deny buttons inline
chat-shape harness UI + session continuity
lake tab → horizontal timeline, color-by-provenance
agentic tool-calling loop scaffold
```
