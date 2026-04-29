"""Tag-keyed renderer registry for timeline view.

Each timeline strip is a chronological run of deltas, with one or more
anchors (the deltas that matched the query) marked. The strip is the
load-bearing unit of recall — replaces the old fragment-per-line view
that lost conversation context.

Render dispatch is by tag prefix: a registry of (key, renderer)
pairs is consulted in order, first match wins. The key matches if the
delta carries the exact tag, a tag with that prefix, or — for the
``source:`` virtual key — has that source. Unmatched deltas fall
through to the default renderer.

Each renderer takes a single delta dict and returns one or more lines
(strings, no trailing newline). Run-length collapsed entries arrive
already shaped as ``kind:collapsed`` with ``count`` / ``t_start`` /
``t_end`` populated; their renderer is just one of the registered
ones, keyed on ``kind:collapsed``.
"""

from __future__ import annotations

from collections.abc import Callable

# A registered key matches a delta when:
#   * the key starts with "source:" — match if delta.source == key[7:]
#   * otherwise — match if any delta tag equals the key, or any tag
#     starts with f"{key}:" (treat the key as a tag-prefix).
# First-match-wins, so most-specific keys go first in the registry.

RendererFn = Callable[[dict], str]

_REGISTRY: list[tuple[str, RendererFn]] = []


def register(key: str, fn: RendererFn) -> None:
    """Append a renderer to the registry. Order matters — earlier
    entries match first. Caller is responsible for ordering specificity."""
    _REGISTRY.append((key, fn))


def _matches(d: dict, key: str) -> bool:
    if key.startswith("source:"):
        return d.get("source") == key[len("source:") :]
    # `kind:<x>` keys also match a top-level `kind` field, since
    # collapsed-run virtual deltas carry their kind there (not in tags).
    if key.startswith("kind:"):
        kind_val = d.get("kind")
        if kind_val and key == f"kind:{kind_val}":
            return True
    tags = d.get("tags") or []
    if key in tags:
        return True
    prefix = f"{key}:"
    return any(t.startswith(prefix) for t in tags)


def render_delta(d: dict) -> str:
    """Pick the first matching renderer; fall back to default."""
    for key, fn in _REGISTRY:
        if _matches(d, key):
            return fn(d)
    return _render_default(d)


# ── Built-in renderers ──────────────────────────────────────────────


def _short_ts(d: dict) -> str:
    """ISO timestamp → HH:MM:SS slice (UTC, no date)."""
    ts = d.get("timestamp") or d.get("t_start") or ""
    if "T" in ts:
        return ts.split("T", 1)[1][:8]
    return ts[:8]


def _content_oneline(d: dict, cap: int = 220) -> str:
    """Compact single line: collapse whitespace, truncate."""
    s = (d.get("content") or "").strip()
    s = " ".join(s.split())
    if len(s) > cap:
        s = s[: cap - 1] + "…"
    return s


def _anchor_marker(d: dict) -> str:
    return "▸" if d.get("is_anchor") else " "


def _render_default(d: dict) -> str:
    src = (d.get("source") or "?").ljust(13)[:13]
    return f"{_anchor_marker(d)} {_short_ts(d)}  {src}· {_content_oneline(d)}"


def _render_collapsed(d: dict) -> str:
    src = d.get("source") or "?"
    count = d.get("count") or 0
    t_start = d.get("t_start") or d.get("timestamp") or ""
    t_end = d.get("t_end") or ""
    s_short = t_start.split("T", 1)[1][:8] if "T" in t_start else t_start[:8]
    e_short = t_end.split("T", 1)[1][:8] if "T" in t_end else t_end[:8]
    return f"  {s_short}  {src} × {count} (through {e_short})"


def _render_dialog(d: dict) -> str:
    """Conversational sources (claude-code, fathom-chat). Show role hint
    when tags carry it, otherwise default shape."""
    tags = d.get("tags") or []
    role = "user" if "user" in tags else "assistant" if "assistant" in tags else None
    src = (d.get("source") or "?").ljust(13)[:13]
    role_str = f" {role}:" if role else ""
    return (
        f"{_anchor_marker(d)} {_short_ts(d)}  {src}·{role_str} {_content_oneline(d)}"
    )


def _render_sediment(d: dict) -> str:
    """Sediment cards — show the first sentence + provenance count."""
    content = (d.get("content") or "").strip().replace("\n", " ")
    first = content.split(".", 1)[0].strip()
    if len(first) > 200:
        first = first[:199] + "…"
    elif len(first) < len(content):
        first += "…"
    n_from = sum(1 for t in (d.get("tags") or []) if t.startswith("from:"))
    src = "sediment".ljust(13)
    suffix = f" (from {n_from} sources)" if n_from else ""
    return f"{_anchor_marker(d)} {_short_ts(d)}  {src}· {first}{suffix}"


def _render_routine_fire(d: dict) -> str:
    rid = next(
        (t.split(":", 1)[1] for t in (d.get("tags") or []) if t.startswith("routine-id:")),
        "?",
    )
    src = "routine".ljust(13)
    return f"{_anchor_marker(d)} {_short_ts(d)}  {src}· [routine {rid} fired]"


def _render_mood(d: dict) -> str:
    state = next(
        (t.split(":", 1)[1] for t in (d.get("tags") or []) if t.startswith("feeling:")),
        None,
    )
    src = "mood".ljust(13)
    label = f"feeling: {state}" if state else _content_oneline(d, cap=80)
    return f"{_anchor_marker(d)} {_short_ts(d)}  {src}· {label}"


# Registration — most-specific first.
register("kind:collapsed", _render_collapsed)
register("kind:routine-fire", _render_routine_fire)
register("kind:sediment", _render_sediment)
register("kind:mood", _render_mood)
register("source:claude-code", _render_dialog)
register("source:fathom-chat", _render_dialog)
# Catch-all dialogic content marked by role tag even on other sources.
register("user", _render_dialog)
register("assistant", _render_dialog)
