"""One process — the unit of thought.

Spawn → produce one voice take → die. Each process picks the next voice
in the rotation, reads its resonance-ranked substrate, calls the LLM,
and writes a thought delta into the puddle tagged `voice:<name>` so
later processes (and the witness) can find it.

Cross-voice convergence is measured in `metric.measure_cross_voice_convergence`
and drives the worker's settle check — voices stop deliberating when their
takes converge below the rolling-window spread threshold.
"""

from __future__ import annotations

from . import resonance
from .intents import CONVO_TAG
from .llm import loop_generate
from .prompts import VOICE_PROMPT, VOICES
from .puddle import puddle


POEM_TTL_S = 48 * 60 * 60       # voice thoughts — 48h rolling horizon

SESSION_TAG_PREFIX = "session:"

# Total deltas to feed the voice as substrate. Matches the experiment's
# LOOP_INPUT_SAMPLE_K=8: 3 voice anchors + ~5 resonant items. Voice anchors
# are always retained; later categories (recall-results, lake-mirrors,
# crystal, mood) compete for the remainder budget in priority order.
INPUT_SAMPLE_K = 8


def _render_seed_block(pending: list[dict]) -> str:
    """Format the open-question(s) the chorus is thinking about."""
    if not pending:
        return "  «(no question — sit with what's already in the substrate)»"
    if len(pending) == 1:
        text = (pending[0].get("content") or "").strip().split("\n")[0][:400]
        return f'  "{text}"'
    lines = []
    for it in pending[:5]:
        text = (it.get("content") or "").strip().split("\n")[0][:300]
        lines.append(f"  · {text}")
    return "\n".join(lines)


async def _gather_substrate(
    session_tag: str,
    voice_name: str,
    pending: list[dict],
) -> list[dict]:
    """Build a voice's substrate from the puddle.

    Voice anchors first (peer awareness), then resonance-ranked
    material from the puddle's broader pool. The resonance signal is
    the voice's own prior thought when one exists, falling back to the
    pending intent text — what this voice has been thinking about, or
    what the user actually asked.

    Order:
      1. Voice anchors — most recent thought per voice (incl. self).
         Always retained; this is how voices read each other across
         rounds.
      2. Resonance-ranked from {recall-results ∪ lake-mirrors}: the
         top items most semantically aligned with the voice's signal.
         A recall pulled by a sibling voice can land here when it
         resonates — that's the cross-pollination point.
      3. Crystal + mood — identity / felt-sense, convo-wide. Land
         only if the resonance pool didn't fill the budget.

    Returns deltas as dicts; caller renders them in the standard
    [source · ts · tags] format alongside the seed.
    """
    picks: list[dict] = []
    seen_ids: set[str] = set()

    def _add(d: dict) -> None:
        did = d.get("id")
        if did and did not in seen_ids:
            picks.append(d)
            seen_ids.add(did)

    # 1. Voice anchors — one most-recent thought per voice (including
    # this one's own prior take, which the resonance signal also reads).
    for v in VOICES:
        for d in puddle.query(
            tags_include=[session_tag, "thought", f"voice:{v['name']}"],
            limit=1,
        ):
            _add(d)

    # 2. Resonance pool. Signal = voice's own prior thought (preferred,
    # because that's where the voice has been thinking) or the intent
    # text (the original ask). On round 0 there's no prior thought, so
    # the intent text grounds resonance against intent-recall content.
    signal_text = ""
    own_prior = puddle.query(
        tags_include=[session_tag, "thought", f"voice:{voice_name}"],
        limit=1,
    )
    if own_prior:
        signal_text = (own_prior[0].get("content") or "").strip()
    if not signal_text and pending:
        signal_text = (
            (pending[0].get("content") or "")
            .split("\n\n[intent-payload]", 1)[0]
            .strip()
        )

    remaining = max(0, INPUT_SAMPLE_K - len(picks))
    if remaining > 0:
        candidates: list[dict] = []
        candidate_ids: set[str] = set()
        # Recall-results from this session — fresh question-pulled
        # material from any voice's searcher.
        for d in puddle.query(
            tags_include=[session_tag, "recall-result"],
            limit=80,
        ):
            did = d.get("id") or ""
            if did and did not in seen_ids and did not in candidate_ids:
                candidates.append(d)
                candidate_ids.add(did)
        # Lake-mirrors — convo-wide ambient lake activity. Resonance
        # filters this hard; without ranking, the recency stream
        # floods substrate with whatever's happening elsewhere.
        for d in puddle.query(
            tags_include=[CONVO_TAG, "lake-delta"],
            limit=80,
        ):
            did = d.get("id") or ""
            if did and did not in seen_ids and did not in candidate_ids:
                candidates.append(d)
                candidate_ids.add(did)

        if candidates:
            ranked = await resonance.rank(signal_text, candidates, top_k=remaining)
            for d in ranked:
                _add(d)

    # 3. Identity fallbacks — only if resonance left budget.
    remaining = max(0, INPUT_SAMPLE_K - len(picks))
    if remaining > 0:
        for d in puddle.query(
            tags_include=[CONVO_TAG, "crystal"],
            limit=remaining,
        ):
            if len(picks) >= INPUT_SAMPLE_K:
                break
            _add(d)

    if len(picks) < INPUT_SAMPLE_K:
        for d in puddle.query(tags_include=[CONVO_TAG, "mood"], limit=1):
            _add(d)

    return picks


def _render_context(deltas: list[dict]) -> str:
    """[source · timestamp · tags]\\ncontent format the loop has used."""
    if not deltas:
        return "(none yet — you are the first voice to speak)"
    blocks = []
    for d in deltas:
        c = (d.get("content") or "").strip()
        if not c:
            continue
        src = d.get("source") or "?"
        ts = d.get("timestamp") or ""
        tags = ", ".join((d.get("tags") or [])[:6])
        blocks.append(f"[{src} · {ts} · {tags}]\n{c}")
    return "\n\n".join(blocks)


async def run_process(
    *,
    pid: str,
    session_tag: str,
    voice: dict[str, str],
    pending: list[dict],
) -> str:
    """Run one voice tick. Writes a thought to the puddle. Returns the
    thought text so the caller can log it.
    """
    substrate = await _gather_substrate(session_tag, voice["name"], pending)
    seed_block = _render_seed_block(pending)
    recent = _render_context(substrate)

    prompt = VOICE_PROMPT.format(
        seed_block=seed_block,
        recent_thoughts=recent,
        voice_name=voice["name"],
        voice_stance=voice["stance"],
        voice_bias=voice["bias"],
    )

    try:
        # max_tokens generous so a voice never gets cut off mid-sentence.
        # The voice prompt asks for "one to three sentences" but the model
        # sometimes runs longer; capping low produced visibly truncated
        # thoughts ("The most beautiful thing I"). 2048 is plenty of
        # headroom — a real voice take rarely exceeds 200 tokens.
        thought = await loop_generate(
            prompt=prompt,
            tier="medium",
            max_tokens=2048,
            temperature=0.95,
        )
    except Exception as e:
        thought = f"(thought call failed: {type(e).__name__})"

    thought = thought.strip(" \"'`*_\n\t")

    if thought:
        await puddle.write(
            content=thought,
            tags=[
                CONVO_TAG, session_tag, f"process:{pid}",
                "thought", f"voice:{voice['name']}",
            ],
            source="voice",
            ttl_seconds=POEM_TTL_S,
        )

    return thought
