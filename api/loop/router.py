"""Output routing layer.

Single decision point for where witness output goes. Takes the model's
route hint and validates/overrides it via the priority ladder and the
engagement crystal.

Priority ladder (most specific → most general):
  suppress       noise / already-addressed — emit nothing
  helper         executable task dispatched to a helper agent
  notification   urgent interrupt → feed alert bell
  feed-card      curated, resonant, bubbled-up content
  chat-reply     floor — ambient and conversational, default

The engagement crystal lives here, not in the feed directive. It informs
routing decisions across all surfaces, not just feed generation.
"""

from __future__ import annotations

import asyncio
import json
import time

from .surfaces import Surface

# Cached crystal so the router doesn't hit the lake on every card.
_CRYSTAL_CACHE: dict = {}
_CRYSTAL_TTL_S = 300.0  # 5 minutes
_CRYSTAL_LOCK: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _CRYSTAL_LOCK
    if _CRYSTAL_LOCK is None:
        _CRYSTAL_LOCK = asyncio.Lock()
    return _CRYSTAL_LOCK


async def get_engagement_crystal() -> dict:
    """Load the current feed-orient crystal from the lake (cached 5 min).

    Returns the parsed crystal object, or {} if unavailable.
    """
    now = time.monotonic()
    if _CRYSTAL_CACHE.get("loaded_at", 0) + _CRYSTAL_TTL_S > now:
        return _CRYSTAL_CACHE.get("crystal", {})

    async with _lock():
        # Re-check after acquiring lock in case another coroutine already
        # refreshed while we were waiting.
        if _CRYSTAL_CACHE.get("loaded_at", 0) + _CRYSTAL_TTL_S > now:
            return _CRYSTAL_CACHE.get("crystal", {})
        try:
            from .. import delta_client

            items = await delta_client.query(
                tags_include=["crystal:feed-orient"],
                limit=1,
            )
            if items:
                raw = (items[0].get("content") or "").strip()
                parsed = _parse_crystal(raw)
                _CRYSTAL_CACHE["crystal"] = parsed
            else:
                _CRYSTAL_CACHE["crystal"] = {}
        except Exception as e:
            print(f"[router] crystal fetch failed: {type(e).__name__}: {e}")
            _CRYSTAL_CACHE.setdefault("crystal", {})
        _CRYSTAL_CACHE["loaded_at"] = time.monotonic()
    return _CRYSTAL_CACHE.get("crystal", {})


def invalidate_crystal_cache() -> None:
    """Force the next routing call to reload the engagement crystal."""
    _CRYSTAL_CACHE.clear()


def _parse_crystal(raw: str) -> dict:
    """Parse the crystal:feed-orient content into a usable dict.

    The crystal may be double-encoded JSON (narrative field is itself
    JSON from an older regen pass).
    """
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        narrative_field = obj.get("narrative") or ""
        if isinstance(narrative_field, str) and narrative_field.strip().startswith("{"):
            try:
                inner = json.loads(narrative_field)
                return {
                    "narrative": inner.get("narrative") or narrative_field,
                    "directive_lines": inner.get("directive_lines")
                    or obj.get("directive_lines")
                    or [],
                }
            except (json.JSONDecodeError, AttributeError):
                pass
        return {
            "narrative": narrative_field,
            "directive_lines": obj.get("directive_lines") or [],
        }
    except (json.JSONDecodeError, AttributeError):
        return {"narrative": raw}


def classify(route_hint: str, tags: list[str] | None = None) -> str:
    """Map a raw route hint to a canonical surface name.

    The model emits route hints like "feed-card", "chat-reply",
    "helper:<host>", "tool:<name>", "alert:<kind>", "unknown".
    This function normalises them to the surface vocabulary without
    making any availability or preference checks — those live in
    validate_route.

    Returns one of: "feed", "chat", "helper", "notification", "suppress".
    """
    tags = tags or []
    hint = (route_hint or "").strip().lower()

    if hint == "suppress" or hint == "unknown":
        return Surface.SUPPRESS

    if hint.startswith("helper:") or hint == "helper":
        return Surface.HELPER

    if hint.startswith("alert:") or hint == "notification":
        return Surface.NOTIFICATION

    if hint == "feed-card" or hint == "feed":
        return Surface.FEED

    # Tool proposals and routine fires are handled upstream; by the time
    # they reach classify they've already been branched. Default them to
    # feed so they render as cards.
    if hint.startswith("tool:") or hint.startswith("routine-fire:"):
        return Surface.FEED

    # Default: chat is the floor.
    return Surface.CHAT


async def validate_route(
    route_hint: str,
    *,
    tags: list[str] | None = None,
    available_helpers: list[dict] | None = None,
    has_title: bool = True,
) -> str:
    """Apply routing rules and return the final route string.

    Keeps the same string protocol as the rest of the stack (e.g.
    "chat-reply", "feed-card", "helper") so callers can drop it
    directly into the witness payload without schema changes.

    Rules applied in order:
      1. Unknown / suppress → "unknown"
      2. helper → only if the named host (or host+role pair) is
         in available_helpers; otherwise fall through to chat-reply.
      3. feed-card → requires a real title; untitled downgrades to
         chat-reply.
      4. alert: → passes through; bust-through is respected.
      5. Everything else → as-is.

    Two helper hint shapes are accepted:
      `helper:<host>`            — host alone; downstream picks a role
      `helper:<role>:<host>`     — explicit role on host
    """
    hint = (route_hint or "chat-reply").strip()

    if hint in ("unknown", "suppress"):
        return "unknown"

    if hint.startswith("helper:") or hint == "helper":
        rest = hint[len("helper:") :].strip() if ":" in hint else ""
        helpers = available_helpers or []
        if not rest:
            return hint
        if ":" in rest:
            role, _, target_host = rest.partition(":")
            role = role.strip()
            target_host = target_host.strip()
            ok = any(h["host"] == target_host and h["role"] == role for h in helpers)
            if not ok:
                pretty = sorted(f"{h['role']}@{h['host']}" for h in helpers)
                print(
                    f"[router] helper:{role}:{target_host} not in available "
                    f"helpers {pretty} — falling back to chat-reply"
                )
                return "chat-reply"
        else:
            target_host = rest
            host_set = {h["host"] for h in helpers}
            if target_host not in host_set:
                print(
                    f"[router] helper:{target_host} not in available hosts "
                    f"{sorted(host_set)} — falling back to chat-reply"
                )
                return "chat-reply"
        return hint

    if hint == "feed-card" and not has_title:
        return "chat-reply"

    return hint
