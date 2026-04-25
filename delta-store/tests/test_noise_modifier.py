"""Unit tests for query._noise_modifier.

The modifier reranks generic, low-information deltas down at search time
without dropping them. Two compounding terms — length penalty and
noise-centroid similarity — fold into a multiplicative distance bump.
Pin the math: a regression here silently degrades every recall, MCP
search, and chat lookup.
"""

from __future__ import annotations

import math

import pytest
from deltas.query import (
    LENGTH_PENALTY,
    LENGTH_THRESHOLD,
    NOISE_ALPHA,
    NOISE_FLOOR,
    _noise_modifier,
)

# A two-dim "centroid" makes similarity easy to reason about: aligning a
# candidate embedding with [1, 0] gives noise_sim = 1, orthogonal gives 0,
# and we can dial in any intermediate via cos(angle).
NOISE = [1.0, 0.0]


def _embedding_at(noise_sim: float) -> list[float]:
    """Return a 2D unit vector with the given cosine similarity to NOISE."""
    angle = math.acos(max(-1.0, min(1.0, noise_sim)))
    return [math.cos(angle), math.sin(angle)]


def test_no_signals_returns_neutral() -> None:
    """Long content + no centroid = no penalty, factor stays 1.0."""
    long_text = "x" * (LENGTH_THRESHOLD + 10)
    assert _noise_modifier(long_text, [], []) == pytest.approx(1.0)


def test_empty_content_skips_length_term() -> None:
    """An empty string has len 0; the `0 < len` guard prevents a phantom
    penalty on image-modality deltas (no caption) or routine fires."""
    assert _noise_modifier("", [], []) == pytest.approx(1.0)
    assert _noise_modifier(None, [], []) == pytest.approx(1.0)
    assert _noise_modifier("   ", [], []) == pytest.approx(1.0)  # whitespace-only


def test_short_content_applies_length_penalty() -> None:
    """Content under LENGTH_THRESHOLD chars takes the fixed length bump."""
    short = "hey"
    assert _noise_modifier(short, [], []) == pytest.approx(1.0 + LENGTH_PENALTY)


def test_length_threshold_is_strict_lower_bound() -> None:
    """At exactly LENGTH_THRESHOLD chars the length term does NOT fire —
    callers can rely on the boundary to tune cutoff vs noise floor."""
    at_threshold = "x" * LENGTH_THRESHOLD
    assert _noise_modifier(at_threshold, [], []) == pytest.approx(1.0)


def test_centroid_below_floor_contributes_nothing() -> None:
    """Embedding orthogonal to the noise centroid (sim=0) is well under
    NOISE_FLOOR — no centroid term applies."""
    long_text = "x" * (LENGTH_THRESHOLD + 10)
    orthogonal = _embedding_at(0.0)
    assert _noise_modifier(long_text, orthogonal, NOISE) == pytest.approx(1.0)


def test_centroid_at_max_similarity_applies_full_alpha() -> None:
    """A delta whose embedding IS the noise centroid (sim=1) takes the
    full NOISE_ALPHA bump — the strongest possible centroid penalty."""
    long_text = "x" * (LENGTH_THRESHOLD + 10)
    identical = _embedding_at(1.0)
    assert _noise_modifier(long_text, identical, NOISE) == pytest.approx(1.0 + NOISE_ALPHA)


def test_centroid_just_above_floor_applies_small_penalty() -> None:
    """The centroid term scales linearly above NOISE_FLOOR. A similarity
    just past the floor should add a tiny but non-zero penalty."""
    long_text = "x" * (LENGTH_THRESHOLD + 10)
    just_above = _embedding_at(NOISE_FLOOR + 0.01)
    factor = _noise_modifier(long_text, just_above, NOISE)
    assert 1.0 < factor < 1.0 + NOISE_ALPHA * 0.1


def test_terms_compound_multiplicatively() -> None:
    """Short content AND embedding near the noise centroid stack — the
    factor is the product of the two independent bumps."""
    short = "hey"
    identical = _embedding_at(1.0)
    expected = (1.0 + LENGTH_PENALTY) * (1.0 + NOISE_ALPHA)
    assert _noise_modifier(short, identical, NOISE) == pytest.approx(expected)


def test_missing_embedding_skips_centroid_term() -> None:
    """A candidate without an embedding (legacy or pending re-embed) only
    pays the length cost — no crash, no spurious centroid score."""
    short = "hey"
    assert _noise_modifier(short, [], NOISE) == pytest.approx(1.0 + LENGTH_PENALTY)


def test_missing_centroid_skips_centroid_term() -> None:
    """If the noise centroid couldn't be built (embedder offline), the
    modifier degrades gracefully to length-only — search still works."""
    short = "hey"
    embedding = _embedding_at(1.0)
    assert _noise_modifier(short, embedding, []) == pytest.approx(1.0 + LENGTH_PENALTY)
