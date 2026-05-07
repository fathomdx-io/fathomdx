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


_HTML_TAG_RE = __import__("re").compile(r"<[a-zA-Z/][^>]*>")
_HTML_ENTITY_RE = __import__("re").compile(r"&(?:nbsp|amp|lt|gt|quot|apos|#\d+);")


def _looks_like_html(s: str) -> bool:
    """Heuristic: content is HTML-laden if it contains a tag-like
    pattern. Avoids false positives on plain text mentioning ``<3``
    or comparison operators by requiring a letter or `/` right after
    the opening ``<``."""
    return _HTML_TAG_RE.search(s) is not None


def _strip_html(s: str) -> str:
    """Cheap HTML stripper for delta content. Drops tags and common
    entities; not a sanitizer — just keeps the readable text. Applied
    universally in ``_content_oneline`` when content looks HTML-laden,
    so ANY source emitting HTML (RSS, any feed, scraped pages, agent
    output that happens to wrap in tags) renders cleanly.
    """
    s = _HTML_TAG_RE.sub(" ", s)
    s = _HTML_ENTITY_RE.sub(" ", s)
    return s


def _content_oneline(d: dict, cap: int = 130) -> str:
    """Compact single line: detect-and-strip HTML, collapse whitespace,
    truncate.

    The 130-char default is tight by design — the witness substrate
    truncates each candidate's full content to 600 chars, so 4–5
    anchor lines need to fit comfortably under that cap. Renderers
    that want more room pass a larger ``cap`` explicitly.
    """
    s = (d.get("content") or "").strip()
    if _looks_like_html(s):
        s = _strip_html(s)
    s = " ".join(s.split())
    if len(s) > cap:
        s = s[: cap - 1] + "…"
    return s


def _anchor_marker(d: dict) -> str:
    return "▸" if d.get("is_anchor") else " "


def _media_suffix(d: dict) -> str:
    """Inline tail marking attached images. Same shape as `_delta_line`
    in search.py and the harness projection, so the model sees one
    consistent `[Image attached: media_hash=<hash>]` token everywhere
    and knows to call see_image when relevant.
    """
    h = (d.get("media_hash") or "").strip()
    return f" [Image attached: media_hash={h}]" if h else ""


def _id_prefix(d: dict) -> str:
    """Show the 12-char delta id on anchor lines so the model has real
    references when it wants to engage / cite / propose constituents.
    Without this, the recall output renders timestamps + sources but
    not ids — the model fabricates id-shaped strings from the format
    it remembers (e.g. "20260420T042833Z-fathom-chat-a1b2c3"), which
    don't resolve in the lake.

    Anchor items only — ambient lines stay clean. They're context,
    not citation material; ids would just clutter the render.
    """
    if not d.get("is_anchor"):
        return ""
    did = (d.get("id") or "").strip()
    if not did:
        return ""
    return f"[{did[:12]}] "


def _render_default(d: dict) -> str:
    src = (d.get("source") or "?").ljust(13)[:13]
    return f"{_anchor_marker(d)} {_short_ts(d)}  {src}· {_id_prefix(d)}{_content_oneline(d)}{_media_suffix(d)}"


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
        f"{_anchor_marker(d)} {_short_ts(d)}  {src}·{role_str} {_id_prefix(d)}{_content_oneline(d)}{_media_suffix(d)}"
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
    return f"{_anchor_marker(d)} {_short_ts(d)}  {src}· {_id_prefix(d)}{first}{suffix}{_media_suffix(d)}"


def _render_routine_fire(d: dict) -> str:
    rid = next(
        (t.split(":", 1)[1] for t in (d.get("tags") or []) if t.startswith("routine-id:")),
        "?",
    )
    src = "routine".ljust(13)
    return f"{_anchor_marker(d)} {_short_ts(d)}  {src}· {_id_prefix(d)}[routine {rid} fired]{_media_suffix(d)}"


def _render_mood(d: dict) -> str:
    state = next(
        (t.split(":", 1)[1] for t in (d.get("tags") or []) if t.startswith("feeling:")),
        None,
    )
    src = "mood".ljust(13)
    label = f"feeling: {state}" if state else _content_oneline(d, cap=80)
    return f"{_anchor_marker(d)} {_short_ts(d)}  {src}· {_id_prefix(d)}{label}{_media_suffix(d)}"


def _render_provenance(d: dict) -> str:
    """Provenance containers — render with their level + constituent
    count + id so the model recognizes them AS named stretches when
    they show up in recall output. Without this, a `kind:provenance`
    delta renders identically to a base moment and the model can't
    naturally tell "this stretch already has a name."

    Catches both proper provenance (L1+) and Q/A markers (L0,
    `kind:qa-marker` + `kind:provenance`). Level surfaces in either
    case, so the model sees the difference between a marker and a
    real episode at a glance.
    """
    tags = d.get("tags") or []
    level = "?"
    for t in tags:
        if isinstance(t, str) and t.startswith("provenance-level:"):
            level = t.split(":", 1)[1]
            break
    n_from = sum(1 for t in tags if isinstance(t, str) and t.startswith("from:"))
    is_qa = any(t == "kind:qa-marker" for t in tags if isinstance(t, str))
    title = ""
    for t in tags:
        if isinstance(t, str) and t.startswith("title:"):
            title = t.split(":", 1)[1].replace("-", " ")
            break
    if not title:
        title = _content_oneline(d, cap=130)
    src = ("qa-marker" if is_qa else "prov").ljust(13)
    did = (d.get("id") or "")[:12]
    badge = f"[L{level} · {n_from} delta{'s' if n_from != 1 else ''} · {did}] "
    return f"{_anchor_marker(d)} {_short_ts(d)}  {src}· {badge}{title}{_media_suffix(d)}"


# Registration — most-specific first.
register("kind:collapsed", _render_collapsed)
register("kind:provenance", _render_provenance)
register("kind:qa-marker", _render_provenance)
register("kind:routine-fire", _render_routine_fire)
register("kind:sediment", _render_sediment)
register("kind:mood", _render_mood)
register("source:claude-code", _render_dialog)
register("source:fathom-chat", _render_dialog)
# Catch-all dialogic content marked by role tag even on other sources.
register("user", _render_dialog)
register("assistant", _render_dialog)
