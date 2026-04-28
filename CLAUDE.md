# fathomdx — conventions

Project-specific conventions, architecture, and lake tag contracts. Read
when working on the Grand Loop, search, routines, or agent plumbing.

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

A loop tick is a parliamentary deliberation feeding a single witness
output. The shape:

1. **Pressure / intents** — `api/loop/pressure.py` watches substrate
   pressure (`api/feed_pressure.py`); when it crosses, `intents.py`
   drops one intent per pass-kind into the puddle. User questions and
   other surface-driven asks also land as intent deltas.
2. **Recall** — `api/loop/recall.py` runs two searcher ticks:
   `run_intent_searcher_tick` fires once per pending intent (grounds
   round-0 voices on the user's literal words); `run_voice_followup_tick`
   fires once per voice per round (each voice refines its own thread).
   Both write `recall-result` deltas into the puddle.
3. **Resonance** — `api/loop/resonance.py` ranks puddle items by
   semantic similarity to whatever a consumer is considering. Voices
   and the witness pull resonance-ranked substrate, not whatever's
   most recent.
4. **Parliament** — `api/loop/process.py` is one process = one voice
   take. Voices iterate in rotation; `api/loop/metric.py` computes
   cross-voice convergence after each tick. When the rolling-window
   spread tightens below `SETTLE_SPREAD_MAX`, the parliament has
   settled.
5. **Witness** — `api/loop/witness.py` reads voice thoughts, threads
   pending intents, asks for one integrated body + a route +
   addressed-intent ids, runs an independent judge for salience /
   novelty / resonance / confidence / comfort. Dual-writes to puddle
   (with `lake-id:<full>` cross-pointer) and lake (TTL'd by default;
   judge axes auto-author when worth keeping).
6. **Telepathy** — `api/loop/telepathy.py` keeps the puddle aware of
   the lake: pulls latest crystal facets, latest mood, mirrors recent
   non-loop activity into the puddle as `lake-delta` items. The
   `recalled-id:<24chars>` tag is the shared dedupe key across
   recall, witness dual-writes, and telepathy mirrors.
7. **Mood + drift + feed-orient** — `api/mood.py`, `api/drift.py`,
   `api/loop/feed_orient*.py` regenerate periodically from accumulated
   substrate. Each lands as a lake delta that telepathy surfaces back
   into the puddle on the next refresh.

The supervisor (`api/loop/worker.py`) ties it together — polls the
puddle for pending intents, runs a parliament round when any are
present, fires the witness, repeats.

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

  * **Search** pulls candidates *into* the puddle from the lake. It's
    intent-anchored, per-voice, and goes through the full plan + rerank
    pipeline. The loop's recall ticks are searches.
  * **Resonance** ranks candidates already in the puddle against a
    signal, returning the top-k. It's a local cosine over already-
    fetched embeddings. Voices and the witness use it to filter their
    substrate; they don't compose queries through it.

Retrieval is not synthesis. Parliament does synthesis. Search and
resonance both feed parliament; neither does the integrating.

## Tag conventions

| Prefix / value | Meaning |
|---|---|
| `kind:sediment` | Distilled take auto-written after a deep recall. Carries `from:<id>` to its sources. |
| `from:<id>` | Provenance pointer. Implicit positive engagement on the target. |
| `affirms:<id>` / `refutes:<id>` | Explicit valence on a target delta. Shifts its rank in future searches. |
| `engages:<id>` / `reply-to:<id>` | Neutral attention pointers. |
| `engagement:more` / `engagement:less` | Feed +/- markers. |
| `kind:routine-fire` / `routine-id:<id>` | Scheduled-prompt fire and its summary pairing key. |
| `voice:<name>` | Loop voice attribution (creator / preserver / destroyer). |
| `chat:<slug>` / `fathom-chat` | Web-chat surface session and source. |
| `recalled-id:<24chars>` | Dedupe key shared across recall, telepathy mirrors, witness dual-writes. |
| `lake-id:<full>` | Puddle → lake cross-pointer on dual-written witness cards. |
| `addresses:<intent-id>` | Witness output marking an intent as resolved. |
| `feeling:<state>` / `kind:mood` | Mood deltas. |
| `crystal:identity` / `crystal:feed-orient` | The two crystals telepathy surfaces. |

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

> Open follow-up: the witness can mint routines from intent deltas
> directly. The OpenAI-shape schema for routine creation lives in
> `api/_tool_schema.py` (`CHAT_ONLY_TOOLS` / `routines` entry); reuse
> it when wiring the witness's routine-fire route.
