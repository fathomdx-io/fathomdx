"""Unit tests for crystal_anchor.cosine_distance.

The helper is shared by api/drift.py and api/feed_crystal.py; a subtle
bug in either branch would skew drift readings across the whole ECG
widget and trigger spurious crystal regens. Pin the math.
"""
from __future__ import annotations

import math

import pytest

from api.crystal_anchor import cosine_distance


def test_cosine_distance_identical_vectors_is_zero() -> None:
    """cos(v, v) = 1, so 1 - cos = 0. Drift against yourself is zero."""
    v = [1.0, 2.0, 3.0]
    assert cosine_distance(v, v) == pytest.approx(0.0)


def test_cosine_distance_orthogonal_is_one() -> None:
    """Perpendicular unit vectors: cos = 0, distance = 1."""
    assert cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)


def test_cosine_distance_opposite_is_two() -> None:
    """Antipodal vectors: cos = -1, distance = 2."""
    assert cosine_distance([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(2.0)


def test_cosine_distance_is_scale_invariant() -> None:
    """Scaling either vector by a positive constant doesn't change the
    distance — it's a direction metric, not a magnitude one."""
    a = [1.0, 2.0, 3.0]
    b = [3.0, 6.0, 9.0]  # same direction, 3× magnitude
    assert cosine_distance(a, b) == pytest.approx(0.0)


def test_cosine_distance_empty_inputs_return_zero() -> None:
    """The helper's explicit degenerate case — used for the 'no anchor
    yet' path where both sides would be []."""
    assert cosine_distance([], []) == 0.0
    assert cosine_distance([1.0], []) == 0.0
    assert cosine_distance([], [1.0]) == 0.0


def test_cosine_distance_mismatched_lengths_return_zero() -> None:
    """Different-dim vectors are always a programming bug, but the
    helper must not crash — it returns 0 so callers treat it as 'no
    drift detected' rather than propagating an exception up the ECG."""
    assert cosine_distance([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


def test_cosine_distance_zero_vector_returns_zero() -> None:
    """A zero vector has no direction; dividing by its norm would raise.
    The helper guards and returns 0 instead."""
    assert cosine_distance([0.0, 0.0, 0.0], [1.0, 2.0, 3.0]) == 0.0


def test_cosine_distance_known_angle() -> None:
    """45-degree angle between [1,0] and [1,1]: cos = 1/sqrt(2) ≈ 0.707,
    so distance ≈ 0.293."""
    assert cosine_distance([1.0, 0.0], [1.0, 1.0]) == pytest.approx(
        1.0 - 1.0 / math.sqrt(2)
    )
