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

import math
import re

from .. import delta_client
from ..search import search as nl_search
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


async def _mirror_target_to_puddle(
    *,
    session_tag: str,
    target: dict,
    event_id: str,
) -> int:
    """Mirror a single user-pointed lake delta into the puddle.

    Used for reply-to: the user clicked a specific delta to respond to,
    so that delta MUST land in substrate even if semantic recall would
    never have surfaced it. The user's pointer IS the relevance — no
    query, no neighborhood expansion, no embedding averaging. Keeps
    the recall-summary pipeline focused on actual query recall.

    Skips short-noise content and re-mirroring of an already-mirrored
    target. Returns 1 if a mirror was written, 0 if skipped.
    """
    content = (target.get("content") or "").strip()
    if not content or _is_recall_noise(target):
        return 0
    original_id = target.get("id") or ""
    recalled_short = original_id[:24] if original_id else ""
    if recalled_short:
        for d in puddle.query(tags_include=[CONVO_TAG, "recall-result"], limit=200):
            for t in d.get("tags") or []:
                if t == f"recalled-id:{recalled_short}":
                    return 0
    original_source = target.get("source") or "unknown"
    tags = [
        CONVO_TAG,
        session_tag,
        "recall-result",
        "mirror",
        f"recall-event:{event_id}",
        "recall-class:reply-target",
        f"from-source:{original_source}",
    ]
    if recalled_short:
        tags.append(f"recalled-id:{recalled_short}")
    await puddle.write(
        content=content,
        tags=tags,
        source=f"recall:{original_source}",
        ttl_seconds=RECALL_TTL_S,
    )
    return 1


async def _averaged_anchor_embedding(anchor_ids: list[str]) -> list[float] | None:
    """Pull anchor deltas, embed their content, return the L2-normalized
    mean vector. Returns None if anything along the way fails — recall
    falls back to letting the puddle's resonance ranker embed the prose
    content itself, which is still useful, just less semantically
    targeted at "what resonated in the lake."
    """
    if not anchor_ids:
        return None
    try:
        deltas = await delta_client.batch_get(anchor_ids)
    except Exception as e:
        print(
            f"[recall] anchor batch_get failed: {type(e).__name__}: {e} — "
            "falling back to content-embed"
        )
        return None
    texts = [(d.get("content") or "").strip()[:1000] for d in deltas]
    texts = [t for t in texts if t]
    if not texts:
        return None
    try:
        embs = await delta_client.embed(texts)
    except Exception as e:
        print(
            f"[recall] anchor embed failed: {type(e).__name__}: {e} — "
            "falling back to content-embed"
        )
        return None
    if not embs:
        return None
    n_dims = len(embs[0])
    if not n_dims:
        return None
    sums = [0.0] * n_dims
    for emb in embs:
        if len(emb) != n_dims:
            # Skip vectors that don't match the leading dimension —
            # safer than padding/truncating and conflating embeddings
            # from different models.
            continue
        for i, v in enumerate(emb):
            sums[i] += float(v)
    avg = [s / len(embs) for s in sums]
    norm = math.sqrt(sum(v * v for v in avg))
    if norm > 0:
        avg = [v / norm for v in avg]
    return avg


async def _write_recall_summary(
    *,
    session_tag: str,
    timelines: list[dict],
    rendered_prose: str,
    event_id: str,
    query: str,
    triggering_voice: str | None = None,
) -> int:
    """Write ONE recall-summary delta into the puddle per fire.

    Replaces the old per-hit mirror pattern (one puddle delta per lake
    hit) with a single narrative entry whose content is the rendered
    timeline strips and whose embedding is the L2-mean of the anchor
    passages that resonated. Voices reading substrate now see coherent
    moments — anchor + ambient context, gap-bounded — instead of the
    spits-and-spurts of orphan fragments the old mirror flow produced.

    Late-chunking shape: content carries the narrative the LLM reads,
    embedding carries the semantic neighborhood the resonance ranker
    matches against. Decoupled because they serve different jobs.

    Dedupe: skip the write if every anchor in this fire was already
    referenced by a prior recall-summary in the same convo. Anchor
    overlap means the substrate already has that resonance; firing
    again would just duplicate the prose.

    Returns 1 if a summary was written, 0 if skipped (no anchors, no
    prose, or full overlap with prior fires).
    """
    if not timelines or not rendered_prose.strip():
        return 0

    # Collect anchor ids across all timelines, preserving order/uniqueness.
    seen_in_fire: set[str] = set()
    anchor_ids: list[str] = []
    for tl in timelines:
        for aid in tl.get("anchor_ids") or []:
            if not aid or aid in seen_in_fire:
                continue
            seen_in_fire.add(aid)
            anchor_ids.append(aid)
    if not anchor_ids:
        return 0

    # Anchor-level dedupe — full overlap = nothing new in this fire.
    existing_short: set[str] = set()
    for d in puddle.query(tags_include=[CONVO_TAG, "recall-result"], limit=200):
        for t in d.get("tags") or []:
            if t.startswith("recalled-id:"):
                existing_short.add(t.split(":", 1)[1])
    new_anchors = [aid for aid in anchor_ids if aid[:24] not in existing_short]
    if not new_anchors:
        return 0

    # Embed by what resonated, not by the prose phrasing.
    avg_emb = await _averaged_anchor_embedding(new_anchors)

    tags = [
        CONVO_TAG,
        session_tag,
        "recall-result",
        "kind:recall-summary",
        f"recall-event:{event_id}",
    ]
    if triggering_voice:
        tags.append(f"for-voice:{triggering_voice}")
    if query:
        tags.append(f"recall-query:{query[:80]}")
    for aid in anchor_ids:
        tags.append(f"recalled-id:{aid[:24]}")

    await puddle.write(
        content=rendered_prose,
        tags=tags,
        source="recall-summary",
        ttl_seconds=RECALL_TTL_S,
        embedding=avg_emb,
    )
    return 1


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
                n = await _mirror_target_to_puddle(
                    session_tag=session_tag,
                    target=target,
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
            result = await nl_search(
                text=query,
                depth="shallow",
                view="timeline",
                limit=RECALL_LIMIT,
            )
        except Exception as e:
            print(f"[intent-searcher] lake search failed: {type(e).__name__}: {e}")
            continue

        n = await _write_recall_summary(
            session_tag=session_tag,
            timelines=result.get("timelines") or [],
            rendered_prose=result.get("as_prompt") or "",
            event_id=event_id,
            query=query,
        )
        if n:
            print(f"[intent-searcher] wrote recall-summary delta into the puddle")
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
        result = await nl_search(
            text=query,
            depth="shallow",
            view="timeline",
            limit=RECALL_LIMIT,
        )
    except Exception as e:
        print(f"[voice-searcher:{voice_name}] lake search failed: {type(e).__name__}: {e}")
        return 0

    n = await _write_recall_summary(
        session_tag=session_tag,
        timelines=result.get("timelines") or [],
        rendered_prose=result.get("as_prompt") or "",
        event_id=event_id,
        query=query,
        triggering_voice=voice_name,
    )
    if n:
        print(f"[voice-searcher:{voice_name}] wrote recall-summary into the puddle")
    return n
