# Harness Topology

A snapshot of the agent harness, its retrieval stack, the provenance
hierarchy, and the producer architecture as of `feat/agent-harness`.

## What the harness is

A drop-in replacement for `witness.run_witness` that turns the
convener+parliament+witness pipeline (deterministic, every fire) into
an agentic tool-calling loop (elective, every fire). The model emits
a JSON envelope each turn — either a tool call or a final response —
and the loop continues until the model elects to respond.

Same return shape as `run_witness`. Lives at `api/loop/harness/`.

## The retrieval stack

Three layers, bottom-up:

1. **Compositional search** (`api/search.py:search()`) — the canonical
   NL recall. A planner LLM composes a multi-step plan over embedding
   similarity; `PlanExecutor` runs it; output is timeline strips around
   hits. All other recall surfaces (chat, MCP, intent-searcher, the
   harness's `semantic` tool) funnel through this.

2. **Provenance graph expansion** — `_expand_sediment_provenance` walks
   `from:` pointers off any provenance-shaped delta (`kind:sediment`
   OR `kind:provenance`) hit and pulls constituents into the same
   result. Single-hop only.

3. **Provenance rerank** — `_apply_valence_rerank` multiplies
   distance by 0.85 for `kind:provenance/sediment` hits and 0.92 for
   Q/A markers. Provenance ranks above raw moments when both match.

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

## The harness's nine tools

```
semantic    expand    ascend    deliberate
state       pattern   time      relate
propose_provenance
```

| tool | shape | what it's for |
|---|---|---|
| `semantic` | `(query, depth)` | content-anchored questions ("tell me about X") via the LLM-composed plan |
| `expand` | `(delta_id)` | walks DOWN: pull a provenance's `from:` children |
| `ascend` | `(delta_id)` | walks UP: find provenance containing a delta |
| `deliberate` | `(question)` | parliament voices on a question; expensive |
| `state` | `(action, ...)` | current attention — pending_intents, proposals, mood, crystal, recent |
| `pattern` | `(action, ...)` | aggregations — tagged, count_by, salient_recent, dormant |
| `time` | `(action, ...)` | temporal-window — between, bucket_by |
| `relate` | `(action, ...)` | engagement/relational — with_contact, engagement, dropped_around, cited_by |
| `propose_provenance` | `(level, title, summary, from_ids, rationale, test_questions)` | draft a `kind:proposal` for human review |

Lens tools (`state`/`pattern`/`time`/`relate`) accept `action="help"`
to enumerate sub-actions. Results from every tool include delta ids
that can be fed into `expand`/`ascend`/`semantic`.

## Producer architecture

Five paths produce provenance, ranging from automatic to deliberate:

| producer | trigger | shape | output |
|---|---|---|---|
| **Q/A marker** | every harness fire with citations | level-0, `kind:qa-marker`, question-anchored | auto-write to lake |
| **propose_provenance (in-situ)** | model-elected, mid-fire | levels 1–3, content-anchored | proposal → dashboard review |
| **Reflective agent** | operator-invoked script | levels 1–3, identity/narrative-shaped | proposal → dashboard review |
| **Topical agent** | operator-invoked script (window or l2-pass) | level-1 episodes / level-2 topics | proposal → dashboard review |
| **Manual** | "let's go" producer-maker session | any level, deep judgment | direct write to lake |

**No agent writes real provenance directly except the manual producer.**
All four other paths produce drafts that land in the dashboard feed
as `kind:proposal tool:provenance`; the operator approves via
Edit/Deny/Approve buttons; the existing proposals.py handler writes
the real `kind:provenance` delta.

## The proposal flow

```
draft (kind:proposal tool:provenance)
   │
   └→ dashboard feed → operator Edit/Deny/Approve
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
3. `kind:mood-shift` — small drift on one affect axis (`+focus 0.1` etc.)
4. `kind:engagement-attest affirms:<id>` — one per `cited_id`
5. `kind:engagement-attest refutes:<id>` — one per `dropped_id`
6. **Q/A marker** (`kind:provenance kind:qa-marker provenance-level:0`)
7. `kind:judge-axes` (background) — salience/novelty/resonance/confidence/comfort
8. `kind:voice-affirmation` (if parliament fired and judge rated above floor)

These shape who Fathom is on the next fire. The next fire's
"recently committed" / "recently concluded" prompt blocks are built
from these writes.

## Visualization surfaces

| URL | what it shows |
|---|---|
| `/ui/harness-test.html` | live trace of one harness fire — context build, each tool call + result, final card; side panel shows recent proposals across all producers |
| `/ui/lake-topology.html` | full provenance hierarchy as horizontal timeline (BIRTH ← time → NOW), level bands top-to-bottom (era / topic / episode / Q/A), nodes positioned at constituent barycenter, sized by source count, colored by level |
| `/ui/index.html` (dashboard) | normal dashboard — feed includes proposals with Edit/Deny/Approve flow; provenance proposals render with title/level/constituent count/rationale/test-questions |

## Output format

The harness response accepts two shapes:

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

Tool calls are always:

```jsonc
{"kind": "tool_call", "tool": "<name>", "args": {...}, "thinking": "<one sentence>"}
```

## What's shipped (commits on `feat/agent-harness`)

Reverse-chronological:

```
aa93dc2 lean chat-reply schema + propose_provenance level constraint + proposals pane
68d43da feat: propose_provenance — model can draft proposals in-situ
fb00987 fix: remove truncation everywhere — show full content
04ff1d7 fix: remove tool-side output truncation for the test page
5d6f6f6 fix: semantic_compositional_search → semantic
293aaad fix: rename search → semantic_compositional_search
96942b9 feat: four lens tools (state/pattern/time/relate)
b489174 feat(ui): lake topology visualization
076156c feat(ui): proposal card renders + edits provenance proposals
45dc542 feat: topical agent for window/topic provenance proposals
5e3def9 feat(proposals): wire reflective-agent proposals to the dashboard
40828d0 fix(search): _expand_sediment_provenance walks kind:provenance too
beb06a9 feat: reflective agent for identity-shaped provenance
3f67939 feat: in-situ Q/A marker after every fire
87fcbef fix: ascend finds kind:provenance, not just kind:sediment
4eb11f4 feat(api): FATHOM_QUIET_MODE skips background lake writers
7866dfa feat: SSE test page with live trace visualizer
ce110ae feat: agentic tool-calling loop scaffold
```

## What's left

Three buckets, in rough leverage order:

### 1. Production wiring

- **Wire harness into `worker.py:_run_one_fire()`** — replace the convener+parliament+witness pipeline (lines ~155-259) with a single `run_harness()` call. Touches the live fathom stack, not just the prov experiment. Once committed, every real fire (chat replies, feed cards, claude-code dispatches) goes through the harness.
- **Delete the old pipeline** — once the harness has been live for a stretch and the convener/process/metric paths aren't called, delete them. Phase 4+ of the River refactor.

### 2. Pressure-based triggering

The reflective and topical agents currently run only when invoked. They should fire automatically when un-sat-with material accumulates ("tiredness").

- **Pressure model for provenance** — count un-provenanced base moments + un-consolidated L1 episodes since last agent run. Above threshold: fire reflective agent (identity-shaped) or topical agent (level-1 batch).
- **Slow-clock supervisor** — like `feed_orient` / `auto_regen` already do, a background task in worker.py that monitors pressure and fires the agents.
- **Locality heuristic** — when firing, prefer regions of recent provenance activity (more provenance has been built there, more is likely to be built there).

### 3. Phase 2b — multi-vector provenance facets

The structural change we sketched but didn't build. Each provenance node carries the embeddings of its direct children as facets, so semantic search finds the parent via constituent content, not just summary content.

- **Schema** — `delta_facets` table (parent_id, child_id, embedding) or multi-row embeddings on the existing deltas table.
- **Sediment / provenance write path** — when `kind:provenance` is written, also write facet rows for each child.
- **Search** — MaxSim-style retrieval over facets; resolve facet hits to their parent provenance.
- **Backfill** — one-shot pass over existing 155 provenance deltas to build facets.

This makes the abstraction-drift problem (LLM-summary embeddings floating away from constituent content) structurally solved rather than worked around via post-recall expansion.

### Smaller items on the queue

- **Q/A marker dedup** — currently one marker per fire even if the same question is asked five times. After N markers on the same question, fold into a level-1 with `from:<marker-id>` × N. Could be a slow-clock pass.
- **`view_full(delta_id)`** escape hatch tool — when a delta is too large to fit in a lens result, the model can fetch the entire content via a dedicated tool.
- **Edit-button polish** for routines — provenance Edit form is in; routines Edit still routes to the wizard. Worth confirming both paths approve the same way.
- **Pressure model UI** — surface the pressure level somewhere in the dashboard (sidebar gauge?) so the operator can see when the system is "tired" and the next reflective fire is imminent.

## Architectural principles

Things we landed on, sometimes accidentally, sometimes by argument:

- **Visible-everything**. The harness shows the model the full standpoint, the full conversation feed, the full tool results — no silent truncation. If results don't fit, the answer is a narrower next call, not a hidden cut.
- **Proposal, not direct write**. Every level-1+ provenance from an agent goes through dashboard review. Q/A markers (level 0, automatic) and Manual (operator-driven) are the only exceptions.
- **Peer tools**. The eight first-tier + one second-tier `propose_provenance` read as siblings in the prompt — no "primary" recall mode. Naming (`semantic` over `search`) does real work here.
- **Lens results feed graph tools**. `state`/`pattern`/`time`/`relate` surface delta ids; `expand`/`ascend`/`semantic` navigate from them.
- **One fire = one self-constituting act**. Beyond the visible card, every fire writes attestation/mood/citations/Q-A-marker. The next fire's identity prompt is partly authored by what the previous fire claimed about itself.
- **Provenance lives in the lake, not in metadata**. Every piece of structure — Q/A markers, episodes, topics, eras — is a `kind:provenance` delta with `from:` pointers. The graph IS the data; no parallel index to keep in sync (until Phase 2b adds facets, which are an enrichment, not a replacement).
- **Producer / approver split**. Producers draft; the operator decides. Multiple producers (Q/A automatic, propose_provenance in-situ, reflective free-association, topical clustering, manual deep-judgment) feed the same dashboard review queue.
