"""Tier-aware LLM config — lake-backed, hot-read.

Each LLM call-site in Fathom belongs to a tier (hard/medium). A tier
resolves to a (provider, model) pair. Picks live in the lake as config
deltas so the UI can edit them live without restarting the api.

  source: fathom-config
  tags:   config:llm:<tier>
  content: {"provider": "openai", "model": "gpt-4o"}

Latest delta wins per tier. If no config delta exists for a tier, the
resolver falls back to env vars (LLM_MODEL_HARD / LLM_MODEL_MEDIUM)
paired with the legacy LLM_PROVIDER, then to PROVIDER_DEFAULTS for the
first configured provider. That ladder means a fresh install with just
`.env` keeps working; an operator who points-and-clicks in the Models
tab gets their picks persisted in the lake.

The resolver caches the latest config per tier for a short window
(CACHE_TTL_SECONDS) so the chat loop doesn't query the lake on every
turn. Writing via set_tier_config() invalidates the cache immediately,
so UI edits apply on the next turn.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from openai import AsyncOpenAI

from . import delta_client
from .providers import get_client
from .settings import PROVIDER_DEFAULTS, settings

log = logging.getLogger(__name__)

CONFIG_SOURCE = "fathom-config"
CONFIG_TAG = "fathom-config"  # broad category tag so /deltas queries can find all config deltas
TIER_TAG_PREFIX = "config:llm:"
VALID_TIERS = ("hard", "medium")

# Cache TTL — short enough that UI edits apply quickly, long enough
# that a busy chat loop doesn't hammer the delta store.
CACHE_TTL_SECONDS = 5.0
_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _env_fallback(tier: str) -> dict[str, str] | None:
    """Derive (provider, model) from env vars. Returns None when the
    env doesn't specify this tier — caller then falls back to defaults.
    """
    # Pick the env-named model for this tier. Legacy LLM_MODEL maps
    # to the hard tier since that's the chat model historically.
    if tier == "hard":
        env_model = settings.model_hard or settings.model
    elif tier == "medium":
        env_model = settings.model_medium
    else:
        return None
    if not env_model:
        return None
    # Env doesn't specify a provider per tier (yet) — pair the model
    # with LLM_PROVIDER.
    return {"provider": settings.provider, "model": env_model}


def _defaults_fallback(tier: str) -> dict[str, str] | None:
    """PROVIDER_DEFAULTS pick for the first configured provider."""
    configured = settings.configured_providers()
    if not configured:
        return None
    # Prefer LLM_PROVIDER's default if it's configured; otherwise the
    # first configured provider.
    candidate = settings.provider if settings.provider in configured else configured[0]
    model = PROVIDER_DEFAULTS.get(candidate, {}).get(tier, "")
    if not model:
        return None
    return {"provider": candidate, "model": model}


async def _read_tier_delta(tier: str) -> dict[str, str] | None:
    """Read the latest config:llm:<tier> delta. Returns parsed
    {provider, model} dict or None if unset/unparseable."""
    try:
        deltas = await delta_client.query(
            tags_include=[CONFIG_TAG, f"{TIER_TAG_PREFIX}{tier}"],
            limit=1,
        )
    except Exception as e:
        log.warning("llm_config: lake query failed for tier=%s: %s", tier, e)
        return None
    if not deltas:
        return None
    raw = deltas[0].get("content") or ""
    try:
        parsed = json.loads(raw)
    except Exception:
        log.warning("llm_config: couldn't parse tier=%s delta content", tier)
        return None
    provider = parsed.get("provider")
    model = parsed.get("model")
    if not (provider and model):
        return None
    return {"provider": provider, "model": model}


async def get_tier_config(tier: str) -> dict[str, str]:
    """Resolve a tier to a {provider, model} dict. Cached. Never raises
    — if nothing resolves, returns an empty dict for the caller to
    handle as "unconfigured"."""
    if tier not in VALID_TIERS:
        return {}
    now = time.monotonic()
    cached = _cache.get(tier)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    picked = await _read_tier_delta(tier)
    if picked is None:
        picked = _env_fallback(tier) or _defaults_fallback(tier) or {}

    # If the picked provider isn't actually configured, fall back
    # further — don't hand the caller a provider it can't speak to.
    if picked.get("provider") and picked["provider"] not in settings.configured_providers():
        fb = _defaults_fallback(tier) or {}
        if fb:
            picked = fb

    _cache[tier] = (now, picked)
    return picked


async def set_tier_config(tier: str, provider: str, model: str) -> dict[str, Any]:
    """Write a new config:llm:<tier> delta. Invalidates the cache so
    the next resolve_tier sees the new pick. Returns the written delta.
    """
    if tier not in VALID_TIERS:
        raise ValueError(f"unknown tier: {tier}")
    if provider not in PROVIDER_DEFAULTS:
        raise ValueError(f"unknown provider: {provider}")
    if not model:
        raise ValueError("model must be non-empty")
    content = json.dumps({"provider": provider, "model": model})
    written = await delta_client.write(
        content=content,
        tags=[CONFIG_TAG, f"{TIER_TAG_PREFIX}{tier}"],
        source=CONFIG_SOURCE,
    )
    _cache.pop(tier, None)
    return written


async def resolve_tier(tier: str) -> tuple[AsyncOpenAI, str]:
    """One-shot helper: pick a (client, model) for a tier. Raises
    LookupError if nothing resolves — the call-site should catch and
    report a clear "no model configured" error rather than a crashed
    500."""
    config = await get_tier_config(tier)
    provider = config.get("provider") or ""
    model = config.get("model") or ""
    if not provider or not model:
        raise LookupError(f"no model configured for tier '{tier}'")
    return get_client(provider), model
