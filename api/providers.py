"""LLM provider adapter — one AsyncOpenAI client, any backend."""
from __future__ import annotations

from openai import AsyncOpenAI

from .settings import settings


def create_client() -> AsyncOpenAI:
    """Build an OpenAI-compat client pointed at the configured provider."""
    return AsyncOpenAI(
        base_url=settings.resolved_base_url,
        api_key=settings.api_key,
    )


llm = create_client()
