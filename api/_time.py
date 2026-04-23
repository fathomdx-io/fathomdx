"""Tiny UTC clock helpers shared across api/ modules.

Every module under api/ had a private `_now() -> datetime` that returned
`datetime.now(UTC)`, sometimes paired with a `_now_iso()` that appended
`.isoformat()`. Consolidating those here removes the duplication and
gives test code a single place to monkey-patch when it wants a frozen
clock.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now() -> datetime:
    """Current UTC time as a timezone-aware datetime."""
    return datetime.now(UTC)


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return now().isoformat()
