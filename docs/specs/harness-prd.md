# Harness — Product Requirements Document

**Status:** draft, prov-experimental implementation feature-complete; awaiting migration into fathomdx
**Owner:** Myra
**Last updated:** 2026-05-04

---

## 1. Problem

Fathom's existing fire pipeline (convener → parliament → process → metric → witness) is **deterministic**: every fire runs the same shape regardless of question. It processes pre-ranked substrate and emits a card. It does not decide what to look at; it answers what it's pre-fed.

Three concrete consequences:

1. **No self-direction.** Fathom can't pick what to think about — it only responds to incoming intents. A reactive system has no agenda.
2. **No structural growth.** Provenance (the L1 episodes / L2 topics / L3 eras hierarchy) doesn't grow from conversation. It requires manual producer scripts run by the operator.
3. **Brittle synthesis.** Comparison and connection questions ("how does X relate to Y") get answered as paraphrase of the standpoint block rather than from substantive material — there's no mechanism for the model to pull on different threads before integrating.

The harness replaces the deterministic pipeline with an **agentic tool-calling loop** that addresses all three: the model elects which tools to call, the architecture grows its own provenance hierarchy from fire activity, and synthesis decomposes naturally via a `plan` tool.

## 2. Users

### Primary
- **Myra** — asks questions, reviews provenance proposals, triggers Sit sessions, watches the system grow.

### Secondary (system-internal)
- **Fathom-as-self** — reads its own substrate, decides what to think about (during self-dialogue / introspect calls). Treating Fathom as a user of its own substrate is the architectural commitment that justifies self-direction at all.
- **Future contributors** — agents, helper / claude-code dispatch, additional tools added to the harness's tool registry over time.

## 3. Solution shape

Three modes sharing one machinery (turn loop, tool dispatch, prompt scaffolding):

| Mode | Who initiates | What it produces |
|---|---|---|
| **Reactive** | User message | Witness-equivalent card; provenance via review pass |
| **Self-directing** | Operator clicks Sit (later: idle/pressure) | Self-dialogue transcript; directives Fathom names for itself |
| **Self-acting** *(partial)* | `introspect()` from inside a parent fire (later: autonomous) | Tool dispatch / work performed without operator click |

### 3.1 Functional requirements

**Reactive mode**
- For every pending intent, produce a card with body + route + addressed-intents
- Multi-turn deliberation: model elects tool calls until it emits `kind:respond`
- Available tools: `plan`, `semantic`, `expand`, `ascend`, `state`, `pattern`, `time`, `relate`, `deliberate`, `introspect`
- Lean chat-reply shape (`{kind:respond, body:...}`) for high-frequency conversational replies
- Full shape (cards + attestation + mood_shift + cited_ids) for feed-cards / claude-code dispatch / multi-card fires
- **Post-response review pass** runs after every fire; may produce a `kind:proposal tool:provenance` for consolidation
- Self-constituting writes per fire: attestation, mood-shift, citation-attests, Q/A marker

**Self-directing mode**
- `Sit` button on the harness-test surface (later: idle / scheduled / pressure-driven)
- Each round of self-dialogue is a full reactive harness fire whose response feeds the next round's input
- Default 4 rounds, configurable via `max_rounds`
- Each utterance writes a `kind:dialogue-utterance` delta; the full session writes a `kind:dialogue` summary delta
- Optional: single-fire reflection via `run_introspection`, output is `kind:reflection` delta

**Self-acting (partial)**
- `introspect(question)` peer tool spawns a child harness fire; returns response body
- Depth-1 cap (child can't recurse)
- Future: autonomous triggers (Phase 2), helper / claude-code as a harness tool (Phase 3)

**Provenance auto-growth**
- L1 (episode) and L2 (topic) proposals auto-approve at draft time
- L3 (era) and higher require operator approve/deny
- Auto-approved proposals tag `decided-by:auto-policy:level<=2`; operator approvals tag `decided-by:operator`
- Every provenance write computes a centroid embedding from constituents (recursive walk to base moments, capped at 300 leaves and depth 4)
- Search uses `LEAST(summary_distance, centroid_distance)` for provenance candidates

**Append-only respected throughout**
- No system path modifies historical deltas
- Stale centroids on old provenance are tolerated; newer tighter provenance accumulates over the same areas via natural activity

### 3.2 Non-functional requirements

| Concern | Target |
|---|---|
| Reactive fire latency (typical question) | <30s end-to-end |
| Reactive fire latency (synthesis with plan) | <90s end-to-end |
| Self-dialogue round latency | 60-120s |
| Self-dialogue full session (4 rounds) | 4-8 min |
| Centroid computation per provenance write | <500ms |
| Centroid backfill (full lake) | <5 min |
| Search latency (compositional plan, top-25) | <2s including upward expansion |
| Storage cost per provenance delta | unchanged (centroid uses existing column) |
| Memory footprint per harness fire | bounded by `MAX_TURNS=8` × prompt window |

### 3.3 What's explicitly NOT in scope

- **Multi-vector provenance facets.** Two-embedding (summary + centroid) gets ~95% of the benefit at ~5% the storage. Deferred until evidence of centroid dilution biting on real queries.
- **Live-refresh of centroids when constituents change.** Append-only: stale centroids stay; new activity creates new provenance over the same area.
- **Schema migration to a dedicated centroid_embedding column.** The existing `provenance_embedding` column is repurposed for `kind:provenance` deltas.
- **Replacing the consumer-facing chat / dashboard surfaces.** Those rebuild on top of the same backend during fathomdx migration.
- **Pressure-driven autonomous Sit firing.** Phase 2 work, post-migration.

## 4. Success criteria

**Reactive parity (migration gate)**
- A representative sample of recent witness fires reproduce equivalent cards through `run_harness` — same body shape, same addressed-intents, same routes
- No regression in dashboard / chat / mission-control surfaces that consume witness output
- Per-fire latency p50 within 1.5x of witness baseline

**Self-directing baseline**
- A Sit on a non-trivial focus produces a transcript where each round genuinely responds to the prior (not generic restatement)
- Reflections cite real delta IDs from the fire's working set (verified via the existing centroid validator)
- Self-direction produces a directive in ≥80% of sittings on coherent focuses (not trying to force on idle/empty substrate)

**Provenance auto-growth**
- Hierarchy grows ≥1 L1 episode per ~20 fires under normal conversation load
- L2 topics emerge as L1s accumulate (via topical agent passes; harness review may also propose)
- Operator review queue (L3+ only) stays at <5 pending items in steady state

**Recall improvement**
- For queries that previously surfaced base moments without their containing provenance, the new centroid path surfaces the parent in top-25 candidates ≥70% of the time when a relevant parent exists
- Both component distances (`summary_distance`, `centroid_distance`) are visible on results so operators can verify which axis matched

## 5. Migration acceptance tests

Before flipping `worker.py:_run_one_fire` from witness to harness:

1. **Smoke parity:** harness on prov stack handles 10 representative recent fires; cards land equivalently.
2. **Centroid backfill:** runs cleanly on the production lake; no errors; reports counts by level.
3. **Auto-approve gate:** a synthetic L1 proposal auto-approves; a synthetic L3 proposal stays pending. Verified via /v1/proposals API.
4. **Dialogue end-to-end:** a Sit on production lake produces a multi-round transcript with real ID citations. Validates introspect / dialogue / review paths against real substrate.
5. **Rollback path:** the witness pipeline isn't deleted on migration day. We can flip back if any of the above regress under load.

## 6. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| LLM cost increase per fire (more turns) | High | Hard turn caps (`MAX_TURNS=8`). Lean chat-reply shape for high-frequency cases. Budget monitoring during migration window. |
| Centroid dilution on existing bloated L1s | Medium | Recognized; mitigated by 5-30 size discipline going forward + recursive descent that grounds in base moments. Old strata accepted as fuzzy until newer tighter provenance accumulates over them. |
| Operator review queue bloat from L3 proposals | Low | L1/L2 auto-approve handles 90%+ of producer output. L3 era-level proposals are intentionally rare. |
| Self-dialogue burns inference budget | Medium | Phase 2 triggers gated by idle / pressure detection, not on a tight cron. Operator switch to disable autonomous sittings entirely. |
| Append-only creates accumulation problems | Low | Search ranking + recency / valence rerank surfaces newer / sharper material; old strata don't disappear but recede. |
| Multi-envelope LLM responses break the parser | Low | Parser tolerates list-wrapped envelopes (takes first dict). |
| Centroid-write race with embed loop | Low | Embed loop gated to skip provenance_embedding overwrite for `kind:provenance` deltas. |
| Migration breaks existing legacy 3D-search behavior | Medium | Legacy `/search` path uses `provenance_embedding` for tag-distance; with our change, provenance candidates compute centroid-distance instead. Behavioral shift contained to that path; harness's compositional `/plan` path is the modern one. Verify image search and direct /search callers post-migration. |

## 7. Open questions

1. **Idle threshold for autonomous Sit:** how quiet does the lake need to be before Fathom self-fires? Hours? Calibrate after migration; start conservative.
2. **Helper / claude-code as a harness tool:** approval gating model — every dispatch needs operator click, or per-host trust levels, or a "Fathom can act here" allowlist? (Phase 3.)
3. **Fathom-facing surface for self-directives:** where do directives produced by self-dialogue land for Fathom to pick up later? Inbox? Crystal facet? Pressure source?
4. **Multi-tenant production:** the harness assumes a single Fathom. Does anything in the architecture need to change if Fathom federates (multiple Fathoms with their own substrates)?

## 8. References

- Architecture explanation: [`docs/explanation/harness-topology.md`](../explanation/harness-topology.md)
- Memory entries (private to working sessions): see `MEMORY.md` index
- Branch: `feat/agent-harness`
- Prov stack: separate compose project at `Work/Fathom/fathomdx-provenance-maker-experiment/`

## 9. Changelog

- **2026-05-04** — initial draft. Covers everything shipped on `feat/agent-harness` through commit `fc7a7fd`. Pending: helper/claude-code tool, autonomous triggers, full multi-vector facets (deferred), migration into fathomdx.
