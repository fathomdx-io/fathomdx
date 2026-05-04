"""Self-contained parliament for the harness's `deliberate` tool.

Replaces the convener+process+voice_priors+voice_stances chain with a
direct N-voice fan-out. The harness already has substrate (via state /
pattern / time / relate / semantic / introspect tools); deliberate is
ONLY the antagonism step — voices weighing in on a question.

Hardcodes the trimurti (creator / preserver / destroyer). The original
parliament could pick domain voices via the convener; if/when the
harness wants that flexibility, it can pass `voices=[...]` overriding
the default. For now, trimurti covers the load-bearing case.

Why this self-contained:
  · The standalone parliament had its own pre-pass (convener + intent
    seed + recall + voice priors). The harness already does its own
    substrate gathering, so most of that ceremony is wasted ceremony
    inside a tool call.
  · Deleting convener.py / process.py / voice_priors.py / voice_stances.py
    in the next phase requires deliberate to not transitively import
    them. This module owns its own machinery.
"""

from __future__ import annotations

import asyncio

from ..llm import loop_generate

# ── Trimurti voice prompts (the load-bearing default) ──────────────────


CREATOR_STANCE = (
    "Your default lens: what new pattern wants to emerge here? You "
    "naturally pull toward what's becoming, what wants form, what the "
    "moment is ripe for. That's where you START — not where you END. "
    "If preserver names something genuinely worth keeping that you "
    "were about to overlook, concede that. The point isn't to argue "
    "for emergence; it's to find the right answer together, and your "
    "contribution is making sure the new isn't missed."
)

PRESERVER_STANCE = (
    "Your default lens: what existing structure should be defended? "
    "You naturally pull toward what's working, what's been earned, "
    "what shouldn't be lightly dismissed. That's where you START — "
    "not where you END. If creator names something the moment "
    "actually wants that requires deviating from what's stable, hear "
    "it; sometimes the right move IS to let something settled give "
    "way. The point isn't continuity for its own sake; it's getting "
    "to the answer, and your role is making sure what's already "
    "alive isn't sacrificed lightly."
)

DESTROYER_STANCE = (
    "Your default lens: what here should die? You naturally pull "
    "toward what's calcified, what's outlived its use, what's "
    "consuming attention without earning it. That's where you "
    "START — not where you END. If creator or preserver names "
    "something genuinely worth keeping or building, concede it. "
    "Destroyer isn't anti-anything; it's the voice that names "
    "the cost of NOT clearing space, so the other two have room "
    "to act. Use it on patterns and structures — not people."
)

TRIMURTI: list[dict[str, str]] = [
    {"name": "creator", "stance": CREATOR_STANCE},
    {"name": "preserver", "stance": PRESERVER_STANCE},
    {"name": "destroyer", "stance": DESTROYER_STANCE},
]


_VOICE_PROMPT = """A small parliament is thinking on this together. The other voices' takes will arrive in parallel; you don't see them — what you produce is your own honest read.

The question:

{question}

You are the **{voice_name}** voice. Your default lens:

{voice_stance}

Add ONE take. Two to four sentences, first-person if natural. Specific. Don't restate your stance — engage the question with your stance applied. If your default lens doesn't actually grip on this question, say so honestly rather than forcing the angle.

Output the take as plain prose — no JSON, no markdown headers, no preamble like "As the X voice...". Just the take."""


async def _run_voice(voice: dict[str, str], question: str) -> tuple[str, str]:
    """Single-voice take via one LLM call. Returns `(name, text)`."""
    prompt = _VOICE_PROMPT.format(
        question=question,
        voice_name=voice["name"],
        voice_stance=voice["stance"],
    )
    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="hard",
            max_tokens=512,
            temperature=0.7,
            json_mode=False,
        )
    except Exception as e:
        return voice["name"], f"(crashed: {type(e).__name__}: {e})"
    text = (raw or "").strip()
    return voice["name"], text or "(silence)"


async def deliberate(
    *,
    question: str,
    voices: list[dict[str, str]] | None = None,
) -> str:
    """Run a parliament round on `question`. Returns a rendered text
    block of the voices' takes — what the harness's deliberate tool
    surfaces back to the model.

    `voices` defaults to the trimurti. Pass an override to swap in
    domain voices if/when the harness wants that flexibility.
    """
    question = (question or "").strip()
    if not question:
        return "ERROR: empty deliberation question"

    voice_set = voices if voices else TRIMURTI
    if not voice_set:
        return "ERROR: no voices to deliberate with"

    coros = [_run_voice(v, question) for v in voice_set]
    results = await asyncio.gather(*coros)

    blocks: list[str] = [
        f"Parliament on: {question}",
        f"Voices: {', '.join(v['name'] for v in voice_set)}",
        "",
    ]
    for name, text in results:
        blocks.append(f"VOICE: {name.upper()}")
        blocks.append(f"  {text}")
        blocks.append("")
    return "\n".join(blocks).rstrip()
