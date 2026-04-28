"""Recall — the searcher ticks.

Two ticks, two roles:

  * `run_intent_searcher_tick` — fires ONCE per intent per process,
    awaited before the parliament round loop starts. Composes a lake
    query directly from the user's pending intent body so voices in
    round 0 read recall-results grounded on the user's literal words,
    instead of speculating in a vacuum.

  * `run_voice_followup_tick` — fires once per voice each round inside
    the parliament loop. Each voice composes its own query from its
    own most recent thought (refining its specific thread), anchored
    by the user's intent. Three voices = three parallel searches, each
    writing into the shared puddle. Cross-pollination happens at
    sample time via resonance: a result one voice pulled may surface
    in another voice's substrate when it aligns with that voice's
    signal.

Both write hits as `recall-result` deltas into the puddle, where they
land in voice and witness substrate via resonance ranking in
process._gather_substrate. Soft-fails on any single-step error — a
hiccup in one voice's fire shouldn't block the others or the loop.
"""

from __future__ import annotations

import re

from .. import delta_client
from .intents import CONVO_TAG
from .llm import loop_generate
from .puddle import puddle


# Recall TTL — matches the rolling 48h horizon every other puddle item
# uses, so a recall surfaced this hour is still resonant 30 hours later.
RECALL_TTL_S = 48 * 60 * 60

# How many recall results to keep per searcher fire. The experiment
# used 5, but voices in our setup actually read recall-results as part
# of their substrate (process._gather_substrate), so a fatter pull gives
# the deliberation more material to draw from across rounds. K=10 is a
# moderate increase — the voice's INPUT_SAMPLE_K=8 budget caps how much
# actually lands in any one prompt, so this just widens the pool.
RECALL_LIMIT = 10

# Skip a query that's identical (post-normalize) to the last one fired
# for the same voice. Per-voice dedupe so two voices that happen to
# settle on similar queries don't lose either; only V re-firing its own
# stable query is wasted.
_last_query_norm_by_voice: dict[str, str] = {}

# Intent-driven searches dedupe by intent id, not query string. Each
# pending intent gets searched at most once per process — once round 0
# has grounded voices on it, repeating the same lake search is wasted
# work. Different from the voice-driven dedupe above because intent
# bodies are stable across rounds while voice thoughts shift.
_searched_intent_ids: set[str] = set()


# Minimum char count for a chat-tagged user message to land in recall.
# Short replies ("ya", "ok", "done", "hello") are conversational
# scaffolding, not durable context — they embed near everything because
# they carry no semantic anchor, so a "Nova" query happily surfaces
# them. Mirrors the assistant-side filter at api/search.py:491 (which
# drops Fathom's own chat replies); together both sides of the chat
# transcript stop polluting the loop's substrate.
MIN_RECALL_CHAT_CHARS = 20


def _is_recall_noise(d: dict) -> bool:
    """Return True if this lake delta should be skipped at recall-write.

    Two cases:
      * Assistant-side fathom-chat replies — Fathom recalling its own
        prior outputs as "memory" creates a feedback loop. Same shape
        the consumer search filter uses.
      * User-side chat messages below MIN_RECALL_CHAT_CHARS — too short
        to anchor a meaningful match; their embedding similarity is
        noise.
    """
    tags = d.get("tags") or []
    if "assistant" in tags and (
        "fathom-chat" in tags or d.get("source") == "fathom-chat"
    ):
        return True
    if "user" in tags and "chat" in tags:
        content = (d.get("content") or "").strip()
        if len(content) < MIN_RECALL_CHAT_CHARS:
            return True
    return False


async def _compose_query_from_intent(kind: str, intent_text: str) -> str | None:
    """Compose a lake search query directly from a pending intent.

    Voice-driven recall composes from a voice's mid-deliberation thought
    — useful for "what should the parliament look at next" but blind to
    the user's literal frame. This composer reads the intent body
    instead, so first-touch queries get grounded against the user's
    actual words before voices speculate.

    The prompt explicitly nudges the composer to treat names as people
    (not system components) and to remember the lake holds personal
    context, not just work-substrate — addressing the framing bias
    where chat-LLM queries narrow to logs/conversations/system-state.
    """
    text = (intent_text or "").strip()
    if not text:
        return None
    # Strip the [intent-payload] JSON suffix — the human prefix is what
    # voices read inline, the searcher should match that.
    text = text.split("\n\n[intent-payload]", 1)[0].strip()
    if not text:
        return None
    prompt = f"""The user just brought this to Fathom's attention (intent kind: {kind}):

  {text[:600]}

What ONE concise lake search query (5–15 words) would surface the most useful prior moments from Fathom's memory to ground a real response?

Fathom's lake holds the user's whole life context — work, family, personal notes, vault entries, prior conversations, photos. If the intent mentions a name, search the name directly (it might be a person, not a system). If it asks about a topic, search the topic. Don't restrict to recent work-context.

Return JUST the query text. No quotes, no preamble, no labels."""
    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="medium",
            max_tokens=120,
            temperature=0.5,
        )
    except Exception:
        return None
    q = (raw or "").strip().strip('"\'`')
    return q if q else None


async def _write_recall_results(
    *,
    session_tag: str,
    results: list[dict],
    event_id: str,
    triggering_voice: str | None = None,
) -> int:
    """Write each recall hit into the puddle as a `recall-result` delta.

    Dedupe: skip if a previous recall in this convo already mirrored
    the same lake delta id. Append-only — the puddle never loses a
    recall, but doesn't re-import duplicates.

    `triggering_voice` (optional): when set, tag each written result
    with `for-voice:<name>` for provenance. Informational only —
    resonance ranking at sample time decides which voice actually sees
    each result; this tag is for diagnostics and doesn't gate access.
    """
    # Collect ids already mirrored as recalls in this convo.
    existing_ids: set[str] = set()
    for d in puddle.query(tags_include=[CONVO_TAG, "recall-result"], limit=500):
        for t in d.get("tags") or []:
            if t.startswith("recalled-id:"):
                existing_ids.add(t.split(":", 1)[1])

    written = 0
    for r in results:
        if isinstance(r, dict) and "delta" in r:
            d = r["delta"] or {}
            klass = r.get("klass") or "first"
        else:
            d = r or {}
            klass = "first"
        content = (d.get("content") or "").strip()
        if not content:
            continue
        if _is_recall_noise(d):
            continue
        original_id = d.get("id") or ""
        recalled_short = original_id[:24] if original_id else ""
        if recalled_short and recalled_short in existing_ids:
            continue
        original_source = d.get("source") or "unknown"
        tags = [
            CONVO_TAG, session_tag,
            "recall-result", "mirror",
            f"recall-event:{event_id}",
            f"recall-class:{klass}",
            f"from-source:{original_source}",
        ]
        if triggering_voice:
            tags.append(f"for-voice:{triggering_voice}")
        if recalled_short:
            tags.append(f"recalled-id:{recalled_short}")
            existing_ids.add(recalled_short)
        await puddle.write(
            content=content,
            tags=tags,
            source=f"recall:{original_source}",
            ttl_seconds=RECALL_TTL_S,
        )
        written += 1
    return written


async def _compose_voice_followup_query(
    *,
    voice_name: str,
    voice_stance: str,
    intent_text: str,
    voice_thought: str,
) -> str | None:
    """Compose a per-voice follow-up query.

    Each voice in the parliament has its own stance and thread; each
    one gets to ask its own question of the lake. The voice's most
    recent thought is the signal — what it's been thinking about. The
    intent stays in scope so the voice doesn't drift entirely off the
    user's actual question.

    Cross-pollination happens at sample-time via resonance: a result
    pulled by voice A may surface in voice B's substrate when it
    aligns with B's signal.
    """
    if not voice_thought and not intent_text:
        return None
    voice_thought_block = voice_thought[:600] if voice_thought else "(no prior thought yet)"
    prompt = f"""You are composing a single lake search query for the **{voice_name}** voice in Fathom's parliament. This voice's stance:

  {voice_stance}

The user's original ask (the durable anchor — don't drift entirely off it):

  {intent_text[:400]}

What this voice has just been thinking:

  {voice_thought_block}

What ONE concise lake search query (5–15 words) would pull material that this voice would find most useful for its NEXT thought — given its stance and where it's wandered? Anchored to the user's ask, but shaped by this voice's particular angle.

Fathom's lake holds the user's whole life — work, family, personal notes, vault, prior conversations, photos. Treat names as potentially people. Don't restrict to recent work-context.

Return JUST the query text. No quotes, no preamble, no labels."""
    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="medium",
            max_tokens=120,
            temperature=0.5,
        )
    except Exception:
        return None
    q = (raw or "").strip().strip('"\'`')
    return q if q else None


async def run_intent_searcher_tick(
    *,
    session_tag: str,
    event_id: str,
    intents: list[dict],
) -> int:
    """Fire a recall pass seeded by pending intent bodies, not voice
    thoughts. Each intent is searched at most once per process —
    grounding voices on the user's literal frame before they speculate,
    instead of waiting for round-1 voices to drive the lake search
    through their own (potentially misframed) interpretation.

    Returns the number of recall-results written across all intents
    searched in this call.
    """
    if not intents:
        return 0
    total_written = 0
    for intent in intents:
        intent_id = intent.get("id") or ""
        if not intent_id:
            continue
        if intent_id in _searched_intent_ids:
            continue
        _searched_intent_ids.add(intent_id)

        text = (intent.get("content") or "").strip()
        if not text:
            continue
        intent_tags = intent.get("tags") or []
        kind = "unknown"
        reply_to_ids: list[str] = []
        for t in intent_tags:
            if t.startswith("kind:"):
                kind = t.split(":", 1)[1]
            elif t.startswith("reply-to:"):
                target = t.split(":", 1)[1].strip()
                if target:
                    reply_to_ids.append(target)

        # Reply-to targets — explicit user-pointed context. The user
        # clicked a specific delta and is responding to it; that delta
        # MUST land in the substrate regardless of what semantic search
        # would otherwise pull. The id can point at either store: the
        # frontend uses puddle ids for transient items (voice thoughts,
        # recall mirrors) and falls through to lake ids for durable
        # ones. Try the puddle first (zero-cost lookup); fall back to
        # the lake.
        for target_id in reply_to_ids:
            target = puddle.get(target_id)
            if target is None:
                try:
                    target = await delta_client.get_delta(target_id)
                except Exception as e:
                    print(f"[intent-searcher] reply-to fetch failed for {target_id[:24]}: {type(e).__name__}: {e}")
                    target = None
            if target:
                n = await _write_recall_results(
                    session_tag=session_tag,
                    results=[{"delta": target, "klass": "reply-target"}],
                    event_id=f"{event_id}-reply-to",
                )
                if n:
                    print(f"[intent-searcher] reply-to:{target_id[:24]} pre-loaded into puddle")
                    total_written += n

        query = await _compose_query_from_intent(kind, text)
        if not query:
            continue

        print(f"[intent-searcher] {kind}: {query[:80]!r}")
        try:
            recall = await delta_client.search(query=query, limit=RECALL_LIMIT)
        except Exception as e:
            print(f"[intent-searcher] lake search failed: {type(e).__name__}: {e}")
            continue

        results = recall.get("results") or []
        n = await _write_recall_results(
            session_tag=session_tag,
            results=results,
            event_id=event_id,
        )
        if n:
            print(f"[intent-searcher] wrote {n} recall-result(s) into the puddle")
        total_written += n
    return total_written


async def run_voice_followup_tick(
    *,
    session_tag: str,
    event_id: str,
    voice_name: str,
    voice_stance: str,
    intents: list[dict],
) -> int:
    """Per-voice follow-up search.

    Each voice composes its own query from its own most recent thought
    (or the intent text if it hasn't spoken yet) and fires its own
    lake search. Results land in the shared puddle as recall-results,
    where resonance ranking at sample time decides which voice (or
    voices) actually surface them in their next-round substrate.

    Per-voice dedupe — if voice V's composed query is identical to its
    previous fire's, skip. Voice U firing a similar query still goes
    through (different voice, separate dedupe slot).
    """
    if not intents:
        return 0

    intent_text = (
        (intents[-1].get("content") or "")
        .split("\n\n[intent-payload]", 1)[0]
        .strip()
    )

    own_thoughts = puddle.query(
        tags_include=[session_tag, "thought", f"voice:{voice_name}"],
        limit=1,
    )
    voice_thought = (own_thoughts[0].get("content") or "").strip() if own_thoughts else ""

    # Round 0: this voice has no thought yet AND the intent-searcher
    # already pre-loaded recalls for the intent. Skip — nothing new to
    # ask. Round 1+: voice has a thought to refine from.
    if not voice_thought:
        return 0

    query = await _compose_voice_followup_query(
        voice_name=voice_name,
        voice_stance=voice_stance,
        intent_text=intent_text,
        voice_thought=voice_thought,
    )
    if not query:
        return 0
    norm = re.sub(r"\s+", " ", query.lower().strip())
    if _last_query_norm_by_voice.get(voice_name) == norm:
        print(f"[voice-searcher:{voice_name}] same query as last — skipping: {query[:60]!r}")
        return 0
    _last_query_norm_by_voice[voice_name] = norm

    print(f"[voice-searcher:{voice_name}] {query[:80]!r}")
    try:
        recall = await delta_client.search(query=query, limit=RECALL_LIMIT)
    except Exception as e:
        print(f"[voice-searcher:{voice_name}] lake search failed: {type(e).__name__}: {e}")
        return 0

    results = recall.get("results") or []
    n = await _write_recall_results(
        session_tag=session_tag,
        results=results,
        event_id=event_id,
        triggering_voice=voice_name,
    )
    if n:
        print(f"[voice-searcher:{voice_name}] wrote {n} recall-result(s) into the puddle")
    return n
