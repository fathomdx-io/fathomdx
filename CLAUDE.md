# fathomdx — conventions

Project-specific conventions, architecture, and lake tag contracts. Read
when working on the Grand Loop, search, routines, or agent plumbing.

## Build the architecture right

This is a new product, not gum and tape. When a problem surfaces, the
first instinct should be "what's fundamentally wrong with the model?",
not "where can I patch the symptom?" Stopgaps are sometimes the right
move under time pressure, but every stopgap should be named as a
stopgap and accompanied by a real fix that's tracked, not forgotten.

A few specific habits that matter:

- **Don't conflate the gauge with the source.** A pressure metric, a
  drift score, a dedup token-overlap heuristic — these are read-side
  measurements. Patching them changes what gets reported, not what's
  actually happening underneath. When something looks wrong, ask
  whether the underlying signal has changed or just the read of it.
- **Prefer prompt-data architecture over write-side schema.** When
  the model needs to know something about the past (recent alerts,
  recent endorsements, who's been talked to), the cheap fix is to
  load that data into the standpoint and render it into the prompt.
  Adding new delta kinds, ack tags, or back-edits to existing deltas
  is usually wrong — the lake is append-only and richly tagged
  already.
- **The lake is append-only.** Forever. No deltas get edited or
  deleted. New facts override old ones by being newer; relevance
  ranking surfaces newer-and-tighter material over older-and-fuzzier.
  When a system path looks like it wants to "update" a delta, that
  path is wrong — write a new delta that points at the old one.
- **Read-side filters before write-side schema.** Almost every "we
  need to track X" question is answerable with a query against
  existing tags + a new lens, not a new tag prefix and a write hook.
  Reach for tags only when the data genuinely doesn't exist anywhere.
- **Backstops are not architecture.** Dispatch-layer dedup, env-flag
  kill-switches, hard caps — these protect against pathological
  cases. They don't replace the right shape underneath.

## Substrate and surfaces

The Grand Loop is the **substrate** — where Fathom thinks. Everything
else is a **surface** — a place where the loop's substrate can be read
or written.

  * **Substrate:** the puddle (consciousness/now, ephemeral) and the
    lake (memory/durable, postgres+pgvector). The loop deliberates in
    the puddle and authors lasting takes into the lake.
  * **Surfaces:** web chat, the dashboard feed, MCP, the CLI, kitty
    routines, the agent's host plugins. Each is a way the user (or
    other processes) talk to the substrate. None of them owns it.

Chat used to be the primary substrate. It isn't anymore — it's a
surface. The `chat:<slug>` tag and the `fathom-chat` source still
exist because the web-chat surface still exists; they just don't
carry the agent's main thinking.

## The Grand Loop

A loop tick is a single agentic harness fire. The model elects its own
deliberation via tool calls, integrates the result, and writes a card.
The convener+parliament+witness pipeline (`process.py`, `metric.py`,
`recall.py`, `telepathy.py`) was retired in the harness migration; the
harness's `deliberate` tool covers the antagonism case when needed.

The shape:

1. **Pressure / intents** — `api/loop/pressure.py` watches substrate
   pressure (`api/feed_pressure.py`); when it crosses, `intents.py`
   drops one intent per pass-kind into the puddle. User questions and
   other surface-driven asks also land as intent deltas.
2. **Harness** — `api/loop/harness/loop.py:run_harness` is a multi-turn
   tool-calling loop. Each turn the model emits either a tool call or a
   final card. Tools cover recall (`semantic`, `expand`, `ascend`),
   structured lenses (`state`, `pattern`, `time`, `relate`), synthesis
   (`plan`, `deliberate`, `introspect`), and action
   (`dispatch_helper`, `mint_routine`, `orient_shift`,
   `propose_provenance`). The fire ends when the model emits `respond`.
   A post-response review pass runs once after the response, with
   `propose_provenance` / `skip` as the only outcomes.
3. **Self-constituting writes** — every fire that produces output
   writes attestation, mood-shift, citation engagement, a Q/A marker
   (level-0 provenance), and judge-axes. The next fire's standpoint is
   partly authored by what the previous fire claimed about itself.
4. **Mood + drift + feed-orient** — `api/mood.py`, `api/drift.py`,
   `api/loop/feed_orient.py` regenerate periodically from accumulated
   substrate. Each lands as a lake delta the next fire's standpoint
   loader picks up.

The puddle still exists — it carries pending intents and the
working-set substrate the harness reads at fire-start. It's no longer
the "deliberation arena" the parliament wrote into; the harness reads
it once at fire start (via `standpoint.current()`) and works from
there. The `recalled-id:<24chars>` tag still dedupes recall results
across surfaces.

### Harness

Single flavor — the threaded harness at `api/loop/harness/threaded.py`,
driven by `api/loop/threaded_supervisor.py`. Native chat-completions
with `role:user` / `role:assistant` / `role:tool` turns and native
`tool_calls`; prompt-cache friendly. Polls `thread.unaddressed` for
work. Tools include `engage_feed`, `see_image`, `mark_addressed`, and
self-continuation via `next_prompt`.

The legacy single-prompt harness (`api/loop/harness/loop.py`) is
retained only as a utility for the `introspect` tool's child fire.
It no longer drives any supervisor; cutover is complete.

`api/loop/witness.py` survives as a utility module
(`_dispatch_card`, `_available_helper_hosts`, `_render_hosts_block`).
`run_witness` itself is unused.

## Search

Recall is canonical and shared: every NL search — MCP `remember`, the
CLI, the web chat's pre-recall layer, the loop's recall hooks — goes
through `api/search.py:search()`.

  * **Shallow:** one semantic pgvector query, single-node tree.
    Used by the loop's intent-searcher and voice-followup ticks.
  * **Deep (default):** medium-tier planner LLM composes a JSON plan;
    `delta-store/deltas/plan.py:PlanExecutor` executes it; results
    walk back as a DAG with associative relations.

**Plan primitives** (`PlanStep` actions):

  * `search` — semantic pgvector
  * `filter` — structured tags / source / time
  * `intersect` / `union` / `diff` — set ops on prior step ids
  * `bridge` — deltas close to BOTH centroids of two prior steps
  * `chain` — search outward from a prior step's centroid
  * `aggregate` — group by week / day / month / tag / source
  * `neighbors` — for each delta in a prior step, pull the temporally-
    surrounding deltas (default ±30 minutes, same source). Use when a
    single hit only makes sense in conversation context.

**Reranking layers** (both apply to shallow AND deep paths):

  * **Noise modifier** (`delta-store/deltas/query.py`) — penalizes
    short content and seed-centroid-aligned generic acks ("yeah",
    "ok", "nvm"). Plan executor over-fetches 2× and reranks before
    trimming so trash doesn't crowd real hits out of the limit.
  * **Valence modifier** (`api/search.py:_apply_valence_rerank`) —
    refuted deltas sink, affirmed / `from:`-cited ones float. Capped
    at ±30%.

**Sediment recursion** — every deep recall synthesizes a `kind:sediment`
delta back into the lake with `from:<id>` provenance pointers. Future
searches retrieve sediment, and `_expand_sediment_provenance` auto-
follows `from:` to surface the cited sources alongside it. Engagement
(`affirms:` / `refutes:`) on a sediment shapes the next synthesis via
the cloud-aware sediment prompt.

## Search vs. resonance

These get conflated. They aren't the same thing:

  * **Search** pulls candidates *into* the puddle from the lake. It
    goes through the full plan + rerank pipeline. The harness's
    `semantic` tool calls into this; so do MCP `remember`, the CLI,
    and any other NL recall surface.
  * **Resonance** (`api/loop/resonance.py`) ranks candidates already
    in the puddle against a signal, returning the top-k. It's a local
    cosine over already-fetched embeddings. The harness uses it to
    rank substrate for prompt rendering; it does not compose queries
    through resonance.

Retrieval is not synthesis. The harness does synthesis. Search and
resonance both feed the harness; neither does the integrating.

## Tag conventions

| Prefix / value | Meaning |
|---|---|
| `kind:sediment` | Distilled take auto-written after a deep recall. Carries `from:<id>` to its sources. |
| `from:<id>` | Provenance pointer. Implicit positive engagement on the target. |
| `affirms:<id>` / `refutes:<id>` | Explicit valence on a target delta. Shifts its rank in future searches. |
| `engages:<id>` / `reply-to:<id>` | Neutral attention pointers. |
| `engagement:more` / `engagement:less` | Feed +/- markers. |
| `kind:routine-fire` / `routine-id:<id>` | Scheduled-prompt fire and its summary pairing key. |
| `voice:<name>` | Voice attribution on `deliberate` tool calls (creator / preserver / destroyer). |
| `chat:<slug>` / `fathom-chat` | Web-chat surface session and source. |
| `session:<id>` | Per-fire session tag — every harness fire gets one; tools, traces, and writes within the fire share it. |
| `recalled-id:<24chars>` | Dedupe key shared across recall, the harness's working set, and dual-writes. |
| `lake-id:<full>` | Puddle → lake cross-pointer on dual-written cards. |
| `addresses:<intent-id>` | Card output marking an intent as resolved. |
| `kind:harness-turn` / `tool:<name>` / `turn:<n>` / `harness-source:legacy\|threaded` | Per-tool-call trace deltas the dashboard's thinking accordion renders. |
| `kind:provenance` / `provenance-level:<n>` / `from:<id>` | Named stretches above base moments — Q/A marker (L0), episode (L1), topic (L2), era (L3+). |
| `kind:proposal` / `tool:<provenance\|routine\|...>` | Pending operator decision; auto-approves at L1/L2 for `tool:provenance`. |
| `feeling:<state>` / `kind:mood` | Mood deltas. |
| `crystal:identity` / `crystal:feed-orient` | The two crystals the standpoint loader surfaces. |

`api/reserved_tags.py` is the authority — anything authority-bearing
passes a gate before write.

## Routines

Scheduled prompts that fire on a local machine via the agent's `kitty`
plugin. A routine lands in the lake as a `routine-fire` delta the
agent picks up and executes by spawning claude-code in a kitty window.
The model in that window writes deltas back tagged with whatever the
routine instructs; the dashboard pairs the fire to its summary by
`routine-id:<id>`.

To see what a routine produced, look at the routines page or search
the lake by `routine-id:<id>`.

The harness can mint routines mid-fire via the `mint_routine` tool
(see `api/loop/harness/tools.py:tool_mint_routine`). It lands as a
`kind:proposal tool:routines` delta awaiting operator approval rather
than an immediate routine write — same approval flow as
`dispatch_helper`. The OpenAI-shape schema lives in
`api/_tool_schema.py` (`CHAT_ONLY_TOOLS` / `routines` entry).
