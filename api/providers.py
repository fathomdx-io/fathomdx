"""LLM provider adapters — one AsyncOpenAI client per configured backend.

Historically Fathom spoke to a single provider via one `llm` client.
Tier-aware config lets different call-sites pick different providers, so
we maintain a small registry keyed by provider name. Each client is
lazy-built the first time it's requested and cached for the process
lifetime — the OpenAI SDK's AsyncClient is cheap to keep around and
expensive to recreate per request.

`llm` stays exported as the legacy client for callers that haven't
moved onto the tier resolver yet. It points at whichever provider
`settings.provider` names (or the first configured provider if the
legacy LLM_PROVIDER/LLM_API_KEY pair isn't set).
"""

from __future__ import annotations

from openai import AsyncOpenAI

from .settings import settings

_clients: dict[str, AsyncOpenAI] = {}


def get_client(provider: str) -> AsyncOpenAI:
    """Return the AsyncOpenAI client for `provider`, building on demand.

    Raises LookupError when the provider has no credentials configured
    — callers should guard with `settings.configured_providers()` or
    fall back to another tier. Empty `api_key` is treated as missing;
    ollama's placeholder key counts as configured because the SDK still
    needs a non-empty string.
    """
    cached = _clients.get(provider)
    if cached is not None:
        return cached
    api_key, base_url = settings.provider_credentials(provider)
    if not api_key or not base_url:
        raise LookupError(
            f"provider '{provider}' is not configured — set its API key "
            f"(and base URL for ollama) in .env"
        )
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    _clients[provider] = client
    return client


def _legacy_default_client() -> AsyncOpenAI:
    """Pick the client `llm` points at for back-compat consumers.

    Prefer settings.provider (what LLM_PROVIDER names) when it has
    credentials; otherwise fall back to whichever provider IS
    configured, first-configured-wins. If nothing is configured yet
    (fresh install before preflight), return a dud client pointed at
    the default provider so imports don't crash — the first real call
    will raise a clean 400.
    """
    configured = settings.configured_providers()
    for candidate in [settings.provider, *configured]:
        if candidate in configured:
            return get_client(candidate)
    # Nothing configured. Build a broken client rather than raising
    # at import time; the api's /health endpoint already reports this
    # to the operator.
    return AsyncOpenAI(
        base_url=settings.resolved_base_url or "https://example.invalid/",
        api_key=settings.api_key or "unconfigured",
    )


llm = _legacy_default_client()
