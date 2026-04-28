"""One process — the unit of thought.

Spawn → produce one voice take → die. Each process picks the next voice
in the rotation, reads the small recent-thought context, calls the LLM,
and writes a thought delta into the puddle tagged `voice:<name>` so
later processes (and the witness) can find it.

The experiment also runs a self-similarity metric and a cross-voice
convergence metric here, used to detect when the parliament has
"settled." For v1 we skip that — settle becomes "we ran N processes,
that's enough." We can add metrics back when tuning calls for it.
"""

from __future__ import annotations

from .intents import CONVO_TAG
from .llm import loop_generate
from .prompts import VOICE_PROMPT, VOICES
from .puddle import puddle


POEM_TTL_S = 48 * 60 * 60       # voice thoughts — 48h rolling horizon
EPHEMERAL_TTL_S = 48 * 60 * 60  # spawn/die markers — match the rolling horizon

SESSION_TAG_PREFIX = "session:"


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


def _recent_voice_takes(session_tag: str, per_voice: int = 1) -> list[dict]:
    """Most recent thought from each voice in this session."""
    out: list[dict] = []
    for v in VOICES:
        takes = puddle.query(
            tags_include=[session_tag, "thought", f"voice:{v['name']}"],
            limit=per_voice,
        )
        out.extend(takes)
    return out


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
    # Spawn marker — useful for the viz to draw a process card.
    await puddle.write(
        content=f'{{"process_id": "{pid}", "voice": "{voice["name"]}"}}',
        tags=[
            CONVO_TAG, session_tag, f"process:{pid}",
            "process-event", "event:spawn",
            f"voice:{voice['name']}",
        ],
        source="controller",
        ttl_seconds=EPHEMERAL_TTL_S,
    )

    # Context: pending intents + each voice's most-recent take. v1 keeps
    # this small; the experiment also pulled resonant material from the
    # vampire tap, which we'll add when that module lands.
    voice_anchors = _recent_voice_takes(session_tag, per_voice=1)
    seed_block = _render_seed_block(pending)
    recent = _render_context(voice_anchors)

    prompt = VOICE_PROMPT.format(
        seed_block=seed_block,
        recent_thoughts=recent,
        voice_name=voice["name"],
        voice_stance=voice["stance"],
        voice_bias=voice["bias"],
    )

    try:
        thought = await loop_generate(
            prompt=prompt,
            tier="medium",
            max_tokens=200,
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

    # Death marker.
    await puddle.write(
        content="",
        tags=[
            CONVO_TAG, session_tag, f"process:{pid}",
            "process-event", "event:die",
            f"voice:{voice['name']}",
        ],
        source="controller",
        ttl_seconds=EPHEMERAL_TTL_S,
    )

    return thought
