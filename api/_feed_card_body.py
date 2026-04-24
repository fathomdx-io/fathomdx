"""Feed-card payload parsing + image validation helpers.

Pulled out of api/feed_loop.py so the loop core stays under the
800-line sanity ceiling. Pure functions — no I/O, no mutable
state. Take a candidate pool + the model output, return the
validated card data.
"""

from __future__ import annotations

import json
import re

from ._feed_candidates import _MARKDOWN_IMG_RE

# How many parse attempts before we give up on a line. The first
# attempt does the real work (search, write); retries are cheap
# re-format nudges. Exported because feed_loop checks this cap
# in the _produce_card retry loop.
MAX_FORMAT_ATTEMPTS = 3


def _strip_fences(text: str) -> str:

    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


# How many parse attempts before we give up on a line. The first attempt
# does the real work (search, write); retries are cheap re-format nudges.
MAX_FORMAT_ATTEMPTS = 3


def _parse_card_payload(text: str) -> dict | None:
    """Try to parse a card payload out of the assistant's final message.

    Returns the parsed dict on success, None if it isn't valid JSON.
    Skip-payloads (`{"skip": true, ...}`) round-trip as-is so the caller
    can distinguish "model deliberately skipped" from "model produced
    garbage."
    """
    raw = _strip_fences(text or "")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _candidate_hashes(pool: list[dict] | None) -> set[str]:
    """Real media_hash values from the pre-fetched candidates.

    Used to drop hallucinated hashes from the model's output — flash models
    sometimes invent plausible-looking hex strings that aren't in the lake.
    """
    out: set[str] = set()
    for d in pool or []:
        h = (d.get("media_hash") or "").strip()
        if h:
            out.add(h)
    return out


def _candidate_image_urls(pool: list[dict] | None) -> set[str]:
    """Image URLs the model was actually shown in the candidates block.

    Extracts every markdown `![...](url)` from candidate content — those
    are the URLs the 🖼[url=…] marks in the directive point at. Anything
    else the model emits as body_image is either fabricated (picsum seeds,
    invented CDN paths) or a confused pick (an article URL that would load
    HTML, not an image). The validator rejects anything outside this set.
    """
    out: set[str] = set()
    for d in pool or []:
        for m in _MARKDOWN_IMG_RE.finditer(d.get("content") or ""):
            out.add(m.group(1))
    return out


def _validate_body_image(
    value: str,
    valid_hashes: set[str],
    valid_urls: set[str],
) -> str:
    """Keep value if it's a known media_hash or a URL seen in candidates; else drop."""
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("http://") or v.startswith("https://"):
        # URL must appear verbatim in one of the candidate deltas — blocks
        # picsum.photos and other LLM-invented placeholders.
        return v if v in valid_urls else ""
    if v in valid_hashes:
        return v
    # Looks like a hash but isn't in the candidate pool — hallucination.
    return ""


def _validate_media_list(
    values: list[str],
    valid_hashes: set[str],
    valid_urls: set[str],
) -> list[str]:
    """Same validation, applied across the media[] array."""
    out: list[str] = []
    for v in values or []:
        kept = _validate_body_image(str(v), valid_hashes, valid_urls)
        if kept:
            out.append(kept)
    return out
