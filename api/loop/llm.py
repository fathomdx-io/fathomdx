"""LLM adapter — bridges the Grand Loop to fathomdx's provider abstraction.

The loop-experiment talks directly to Gemini's `genai` SDK via
`generate_content`. Fathomdx uses OpenAI-compatible AsyncOpenAI clients
through `api/providers.py`, so the loop's call sites need a thin
adapter that:

  * formats the prompt as a chat-completions `messages` array
  * picks the right tier (medium for thoughts, hard for witness/judge)
  * threads `response_format={"type":"json_object"}` for the JSON-mode
    paths (witness, judge, search-query composition)
  * caps concurrency to keep parallel voice batches from bursting past
    the per-second ceiling
  * retries on rate-limit-shaped errors with exponential backoff

The loop's `thinking_config=ThinkingConfig(thinking_budget=0)` knob
(Gemini's "skip the deliberation phase" toggle for fast thought calls)
has no equivalent in the OpenAI-compat surface — the medium-tier model
is already the cheap one, so we just don't pass it.
"""

from __future__ import annotations

import asyncio
import os

from openai import AsyncOpenAI

from .. import providers
from ..settings import settings


# Concurrency cap — same intent as the experiment's _LLM_SEM. Parliament
# mode runs voices serially (one tick = one voice), so the cap mostly
# matters when the witness fires alongside late-arriving voice ticks
# or when ambient/pressure pulses overlap. 6 is comfortable for that.
_LLM_SEM = asyncio.Semaphore(int(os.getenv("LOOP_LLM_CONCURRENCY", "6")))

# Hints that an exception is rate-limit-shaped. The OpenAI SDK raises
# RateLimitError directly; other providers may surface 429s as HTTPError
# subclasses with messages we sniff for. We catch by string-match because
# the SDK's exception hierarchy varies across providers.
_RATE_LIMIT_HINTS = (
    "429", "RESOURCE_EXHAUSTED", "quota", "rate limit",
    "rate_limit", "Too Many Requests", "RateLimitError",
)


def _is_rate_limit(exc: BaseException) -> bool:
    s = f"{type(exc).__name__} {exc}".lower()
    return any(h.lower() in s for h in _RATE_LIMIT_HINTS)


def _resolve_client_and_model(tier: str) -> tuple[AsyncOpenAI, str]:
    """Pick the AsyncOpenAI client + model for `tier`.

    `tier` is "medium" (cheap, parallel-safe — used by voices, searcher,
    intent-shaping) or "hard" (witness, judge — single call per fire,
    quality matters). The settings module exposes both as resolved
    properties; the legacy `model` field maps to hard for back-compat.
    """
    client = providers.get_client(settings.provider)
    if tier == "hard":
        return client, settings.resolved_model_hard
    return client, settings.resolved_model_medium


async def loop_generate(
    *,
    prompt: str,
    tier: str = "medium",
    max_tokens: int = 200,
    temperature: float = 0.95,
    json_mode: bool = False,
    max_retries: int = 4,
) -> str:
    """Run one LLM call and return the response text.

    `json_mode=True` requests structured JSON output via OpenAI-compat
    `response_format`. Most providers honor this; the witness/judge
    callers strip preambles defensively in case a provider returns text
    with leading "Here is the JSON:" framing.
    """
    client, model = _resolve_client_and_model(tier)
    request: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        request["response_format"] = {"type": "json_object"}

    delay = 1.0
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        async with _LLM_SEM:
            try:
                resp = await client.chat.completions.create(**request)
                content = (resp.choices[0].message.content or "") if resp.choices else ""
                return content.strip()
            except Exception as e:
                last_exc = e
                if not _is_rate_limit(e) or attempt >= max_retries:
                    raise
        # Sleep outside the semaphore so other in-flight calls aren't blocked.
        await asyncio.sleep(delay)
        delay = min(delay * 2, 16.0)
    if last_exc:
        raise last_exc
    raise RuntimeError("loop_generate exhausted retries without exception")
