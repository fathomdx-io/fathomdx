"""Small helpers for the delta-tag convention shared across modules.

Fathom tags are flat strings like ``contact:bob``, ``chat:quiet-sly-otter``,
``directive-line:physics``. Plenty of code across api/ had near-identical
inline loops that all boiled down to "find the first tag with this prefix
and return the part after the colon." Centralise those here so the
non-string-robustness and prefix-length bookkeeping live in one place.
"""

from __future__ import annotations

from collections.abc import Iterable


def tag_suffix(tags: Iterable[object] | None, prefix: str) -> str | None:
    """Return the suffix of the first tag matching ``prefix``, or None.

    Guards against non-string entries in the tag list (older deltas
    occasionally carried dict metadata that slipped into `tags`) —
    callers don't have to re-implement the isinstance check each time.

    Examples:
        >>> tag_suffix(["chat:s1", "contact:bob"], "contact:")
        'bob'
        >>> tag_suffix(["chat:s1"], "contact:")  # returns None

    The prefix MUST include the trailing colon — passing "contact"
    without the colon would match anything starting with those seven
    characters, which is almost never what the caller wants.
    """
    if not tags:
        return None
    for t in tags:
        if isinstance(t, str) and t.startswith(prefix):
            return t[len(prefix) :]
    return None


def has_any_tag_with_prefix(tags: Iterable[object] | None, prefix: str) -> bool:
    """True iff at least one tag starts with ``prefix``.

    Same non-string tolerance as tag_suffix. Kept separate so callers
    who don't need the suffix don't pay for the None vs. str-return
    distinction.
    """
    if not tags:
        return False
    return any(isinstance(t, str) and t.startswith(prefix) for t in tags)
