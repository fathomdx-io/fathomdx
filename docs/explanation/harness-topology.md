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

2. **Provenance graph expansion** — TWO synthetic steps run after
   every deep search:
   - `_expand_sediment_provenance` walks DOWN: any `kind:sediment` or
     `kind:provenance` hit pulls its `from:` children into the result.
     Single-hop.
   - `_expand_upward_to_provenance` walks UP: every surfaced delta
     finds its containing provenance (recursive up to 3 levels —
     base → L1 episode → L2 topic → L3 era) via a 60s-cached
     child→parent reverse index. Symmetric to the downward walk;
     a base-moment hit now lands with its full provenance stack
     available, no `ascend` call needed.

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
enforces this by deriving min level from constituents (looks up each
`from_id`, checks its `provenance-level:` tag, requires proposal level
> max child level).

## The harness's nine tools

```
semantic    expand    ascend    deliberate
state       pattern   time      relate
propose_provenance
```

| tool | shape | what it's for |
|---|---|---|
| `semantic` | `(query, depth)` | content-anchored questions ("tell me about X") via the LLM-composed plan. Renamed from `search` to make the model think about whether semantic-similarity is actually the right axis. |
| `expand` | `(delta_id)` | walks DOWN: pull a provenance's `from:` children |
| `ascend` | `(delta_id)` | walks UP: find provenance containing a delta |
| `deliberate` | `(question)` | parliament voices on a question; expensive |
| `state` | `(action, ...)` | current attention — pending_intents, proposals, mood, crystal, recent |
| `pattern` | `(action, ...)` | aggregations — tagged, count_by, salient_recent, dormant |
| `time` | `(action, ...)` | temporal-window — between, bucket_by, **around** (anchor+context strip around any delta_id, gap-bounded) |
| `relate` | `(action, ...)` | engagement/relational — with_contact, engagement, dropped_around, cited_by |
| `propose_provenance` | `(level, title, summary, from_ids, rationale, test_questions)` | draft a `kind:proposal` for human review |

Lens tools (`state`/`pattern`/`time`/`relate`) accept `action="help"`
to enumerate sub-actions. Results from every tool include delta ids
that can be fed into `expand`/`ascend`/`semantic`. **Every tool also
returns full untruncated content** — the harness's design principle is
visible-everything; the prompt-budget cap is in `render_tool_history`,
not in the tool returns.

`time(action="around", delta_id, gap_minutes=30)` closes the
shape-inconsistency between `semantic` (which returns timeline strips)
and the other tools (which return raw matches). The model can call it
after any lens hit to get the same anchor+context dressing.

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

## How the harness is told to work

The system prompt (`api/loop/harness/prompts.py`) carries explicit
guidance the model reads each turn:

- **Visible-everything**: full standpoint, full conversation feed,
  full tool results — no silent truncation
- **Synthesis guard**: comparison/connection/synthesis questions ("X
  and Y", "compare", "connections between") MUST decompose. Pull X
  separately, pull Y separately, optionally `deliberate`, then
  respond. The standpoint shows what's *committed* lately, not what's
  *structurally true*; one-shot synthesis questions just paraphrase
  the standpoint.
- **Lean chat-reply**: `{kind: "respond", body: "..."}` is the
  high-frequency case. No need to fill out kicker/title/tail/route
  for a conversational reply. Full schema stays available for
  feed-cards / proposals / multi-card / claude-code dispatch.

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

Tool calls are always:

```jsonc
{"kind": "tool_call", "tool": "<name>", "args": {...}, "thinking": "<one sentence>"}
```

## Visualization surfaces

| URL | what it shows |
|---|---|
| `/ui/harness-test.html` | **Chat-shape harness page**. User questions → assistant bubbles, kicker/title/body/tail rendered cleanly. Session continuity ("the river") — questions in the same session build on each other via the conversation feed. New Chat resets. Lake tab beside it. Activity disclosure under each assistant bubble holds SEED/STANDPOINT/CONTEXT/tool_call/tool_result/attestation. Live stage labels in the "thinking…" placeholder so you see what the harness is doing now. Side panel polls every 4s for harness/reflective/topical proposals across producers. |
| `/ui/harness-test.html` Lake tab | Per-fire visualization. Horizontal timeline (BIRTH ← TIME → NOW). Resonant deltas above the line, colored by their containing provenance. Provenance bands below the line spanning their time ranges. Legend on the right. Empty until a fire surfaces something — the lake answers the current question, not a static structure. |
| `/ui/lake-topology.html` | Standalone analytical topology view of all provenance. Era / topic / episode / Q-A bands top-to-bottom, nodes positioned at constituent barycenters, sized by source count. Click for details; toggle parent edges and Q-A markers. |
| `/ui/lake-sketch.html` | Standalone pencil-on-paper rendering — mirrors Myra's original notebook sketch. BIRTH/NOW arrows, density-of-recall diagonal, sketchy provenance circles, real moment fibers along the timeline. |
| `/ui/index.html` (dashboard) | Normal dashboard — feed includes proposals with Edit/Deny/Approve flow; provenance proposals render with title/level/constituent count/rationale/test-questions. |

## What's shipped (commits on `feat/agent-harness`)

Reverse-chronological:

```
d015564 feat(harness): synthesis guard — decompose comparison/connection questions
a216217 feat(ui): live stage labels in the thinking placeholder
3ec56b1 feat: chat-shape harness UI + session continuity (the river)
3a04626 feat(ui): Lake tab → horizontal timeline, color-by-provenance, legend
234dd8b feat: real moment fibers + time.around for anchor+context shape
0a0ea51 feat(ui): lake sketch view — pencil-on-paper, mirrors Myra's notebook
d89910c docs: harness topology — what's shipped, what's left
aa93dc2 feat(harness): lean chat-reply + level constraint + proposals pane
68d43da feat(harness): propose_provenance — model can draft proposals in-situ
fb00987 fix(harness): remove truncation everywhere — show full content
04ff1d7 fix(harness): remove tool-side output truncation for the test page
5d6f6f6 fix(harness): semantic_compositional_search → semantic
293aaad fix(harness): rename search → semantic_compositional_search
96942b9 feat(harness): four lens tools for non-semantic recall modes
b489174 feat(ui): lake topology visualization
076156c feat(ui): proposal card renders + edits provenance proposals
45dc542 feat(harness): topical agent for window/topic provenance proposals
5e3def9 feat(proposals): wire reflective-agent proposals to the dashboard
40828d0 fix(search): _expand_sediment_provenance walks kind:provenance too
beb06a9 feat(harness): reflective agent for identity-shaped provenance
3f67939 feat(harness): in-situ Q/A marker after every fire
87fcbef fix(harness): ascend finds kind:provenance, not just kind:sediment
4eb11f4 feat(api): FATHOM_QUIET_MODE skips background lake writers
7866dfa feat(harness): SSE test page with live trace visualizer
ce110ae feat(harness): agentic tool-calling loop scaffold
```

## What's left

Three buckets, in rough leverage order:

### 1. Production wiring

- **Wire harness into `worker.py:_run_one_fire()`** — replace the convener+parliament+witness pipeline (lines ~155-259) with a single `run_harness()` call. Touches the live fathom stack, not just the prov experiment.
- **Delete the old pipeline** — once the harness has been live for a stretch and the convener/process/metric paths aren't called, delete them.

### 2. Pressure-based triggering

The reflective and topical agents currently run only when invoked. They should fire automatically when un-sat-with material accumulates ("tiredness").

- Pressure model for provenance — count un-provenanced base moments + un-consolidated L1 episodes since last agent run
- Slow-clock supervisor in worker.py
- Locality heuristic — prefer regions of recent provenance activity

### 3. Phase 2b — multi-vector provenance facets

The structural change we sketched but didn't build. Each provenance node carries the embeddings of its direct children as facets, so semantic search finds the parent via constituent content, not just summary content.

- Schema — `delta_facets` table or multi-row embeddings
- Sediment / provenance write path
- Search MaxSim resolver
- Backfill over existing provenance

### Smaller items on the queue

- **`plan(question)` tool** — for harder synthesis questions, give the model a tool that decomposes a question into sub-questions before tool-calling. Turn the prompt-only synthesis guard into a structural step. Worth building if the prompt fix doesn't hold up on harder synthesis.
- **Q/A marker dedup** — fold N markers on the same question into a level-1 provenance (slow-clock).
- **`view_full(delta_id)`** escape-hatch tool — fetch a single delta's complete content when a lens result truncated it.
- **Standpoint trim for synthesis** — when the question is multi-domain, trim the standpoint block so the model can't paraphrase the recently-committed list (force it to actually pull material).
- **Pressure model UI** — surface the pressure level in the dashboard.

## Architectural principles

Things we landed on, sometimes accidentally, sometimes by argument:

- **Visible-everything**. The harness shows the model the full standpoint, full conversation feed, full tool results — no silent truncation. If results don't fit, the answer is a narrower next call, not a hidden cut.
- **Proposal, not direct write**. Every level-1+ provenance from an agent goes through dashboard review. Q/A markers (level 0, automatic) and Manual (operator-driven) are the only exceptions.
- **Peer tools**. The nine tools read as siblings in the prompt — no "primary" recall mode. Naming (`semantic` over `search`) does real work here.
- **Lens results feed graph tools**. `state`/`pattern`/`time`/`relate` surface delta ids; `expand`/`ascend`/`semantic`/`time(around)` navigate from them.
- **One fire = one self-constituting act**. Beyond the visible card, every fire writes attestation/mood/citations/Q-A-marker. The next fire's identity prompt is partly authored by what the previous fire claimed about itself.
- **The river**. Questions within a session share a `session_tag` so the conversation feed builds up. Each new question lands inside the prior context, not in a vacuum.
- **Provenance lives in the lake, not in metadata**. Every piece of structure — Q/A markers, episodes, topics, eras — is a `kind:provenance` delta with `from:` pointers. The graph IS the data; no parallel index to keep in sync (until Phase 2b adds facets, which are an enrichment, not a replacement).
- **Producer / approver split**. Producers draft; the operator decides. Multiple producers (Q/A automatic, propose_provenance in-situ, reflective free-association, topical clustering, manual deep-judgment) feed the same dashboard review queue.
- **Synthesis questions deserve more turns**. Comparison/connection questions decompose: pull each named entity separately, optionally deliberate, then respond. The standpoint's recently-committed entries shouldn't be the substrate for "X relates to Y" — they're the substrate for "X is what I've been thinking about."
- **Diagonal recall is metaphor, not implementation**. The original sketch's density-of-recall + identity-skew diagonal isn't computed anywhere. The system has many recall modes (nine tools), and the diagonal is one possible projection. The Lake tab visualization respects this — it's a horizontal timeline with provenance as color, not a literal diagonal.
