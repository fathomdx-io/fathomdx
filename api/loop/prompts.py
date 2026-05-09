"""Grand Loop prompts — judge axis scoring.

The parliament prompts (THOUGHT_PROMPT, VOICE_PROMPT, CONVENER_PROMPT,
WITNESS_PROMPT) were retired in the harness migration. The harness has
its own prompts in `harness/prompts.py`; deliberate has its own
trimurti prompts in `harness/deliberate.py`. The judge's independent-
rater shape persists because the harness still calls it on every fire.
"""

from __future__ import annotations

JUDGE_PROMPT = """You are a separate, independent rater scoring a single feed card produced by Fathom. You did not write the card. You only describe it. Rate it on five axes, each in [0.0, 1.0]:

  · salience    — how much this matters to the user RIGHT NOW. (0 = irrelevant; 1 = piercing must-see.)
  · novelty     — how much this is genuinely new vs. paraphrasing prior surfaces. (0 = pure rehash; 1 = first time.)
  · resonance   — how well this lands against the user's current shape and recent moves. (0 = wrong key; 1 = lands deep.)
  · confidence  — how grounded this card's claims feel in real substrate vs. hand-wavy. (0 = cardboard; 1 = load-bearing.)
  · comfort     — what register this card sits in. (0 = piercing/uncomfortable; 1 = warm/affirming.)

You'll see:

KICKER: {kicker}
TITLE: (extracted from the card)
BODY: {body}
SEED: the question or pressure that triggered this card — {seed}

Respond with ONE JSON object on ONE line, exactly this shape:

{{"salience": 0.X, "novelty": 0.X, "resonance": 0.X, "confidence": 0.X, "comfort": 0.X}}

No commentary. No preamble. No code-fences. No markdown. The score map and nothing else."""
