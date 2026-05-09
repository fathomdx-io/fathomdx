"""Friendly summaries of LLM-provider failures.

When a model call exhausts retries (rate limits, depleted credits,
network errors), surfaces that fail need a consistent, readable
representation so each one (chat bubble, OpenAI envelope, routine
summary, mood card) can render the same underlying truth in its own
shape.

This module is the one place that knows how to:
  · pull a one-liner out of a provider exception body, and
  · label the failing call by role (the loop tier or a custom name).

Callers shouldn't string-format exceptions themselves.
"""

from __future__ import annotations

from typing import Any

# Tier → label that lands wherever we render the failure.
_ROLE_LABELS = {
    "hard": "Standard tasks model",
    "medium": "Light tasks model",
}


def _extract_message(exc: BaseException) -> str:
    """Pull a one-liner out of a provider exception body.

    Handles two body shapes:
      OpenAI / Anthropic: `{error: {message}}` dict.
      Gemini OpenAI-compat: `[{error: {message}}, ...]` list of dicts.
    Falls through to str(exc) when neither applies. Caps at 280 chars.
    """
    body: Any = getattr(exc, "body", None)
    candidates: list[dict] = []
    if isinstance(body, dict):
        candidates = [body]
    elif isinstance(body, list):
        candidates = [b for b in body if isinstance(b, dict)]
    for b in candidates:
        err = b.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.split("\n", 1)[0].strip()[:280]
    s = str(exc)
    return s.split("\n", 1)[0].strip()[:280]


def role_for_tier(tier: str) -> str:
    return _ROLE_LABELS.get(tier, f"{tier} tier")


def summarize(*, tier: str, exc: BaseException, role: str | None = None) -> dict:
    """Standardize an LLM-call failure for downstream rendering.

    Returns:
        {
            "role": "Standard tasks model",
            "class": "RateLimitError",
            "message": "Your prepayment credits are depleted.",
        }
    """
    return {
        "role": role or role_for_tier(tier),
        "class": type(exc).__name__,
        "message": _extract_message(exc),
    }
