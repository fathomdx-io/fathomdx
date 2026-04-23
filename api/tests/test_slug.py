"""Session-slug generation tests.

The slug grammar is adj-adj-animal with slot uniqueness (adj1 != adj2).
These invariants are load-bearing: if the generator ever emits an empty
adjective slot or a duplicated adjective, chat-session URLs turn into
collisions that the lake can't disambiguate.
"""
from __future__ import annotations

import random
from unittest.mock import MagicMock

from api.slug import (
    ADJECTIVES,
    ANIMALS,
    generate_slug,
    generate_unique_slug,
    is_slug_taken,
)


def test_generate_slug_shape() -> None:
    """adj-adj-animal grammar — exactly three hyphen-separated parts."""
    slug = generate_slug(random.Random(42))
    parts = slug.split("-")
    assert len(parts) == 3
    assert parts[0] in ADJECTIVES
    assert parts[1] in ADJECTIVES
    assert parts[2] in ANIMALS


def test_generate_slug_adjectives_are_distinct() -> None:
    """Slot uniqueness: adj1 must never equal adj2, even across many draws."""
    r = random.Random(0)
    for _ in range(500):
        adj1, adj2, _animal = generate_slug(r).split("-")
        assert adj1 != adj2


def test_generate_slug_is_deterministic_with_seeded_rng() -> None:
    """Same seed → same slug. Guards against anyone swapping `random` to
    a source that ignores the rng argument (a mistake that would make
    testing basically impossible)."""
    a = generate_slug(random.Random(7))
    b = generate_slug(random.Random(7))
    assert a == b


def test_is_slug_taken_false_on_empty_lake() -> None:
    """Empty results → slug is free."""
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = []
    client.get.return_value = response

    assert is_slug_taken(client, "http://lake", {}, "quiet-sly-otter") is False


def test_is_slug_taken_true_when_delta_exists() -> None:
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = [{"id": "abc"}]
    client.get.return_value = response

    assert is_slug_taken(client, "http://lake", {}, "quiet-sly-otter") is True


def test_is_slug_taken_fails_open_on_lake_error() -> None:
    """If the lake is unreachable, we can't tell — assume free. The
    alternative is blocking on a down lake, which would break onboarding."""
    client = MagicMock()
    response = MagicMock()
    response.status_code = 500
    client.get.return_value = response

    assert is_slug_taken(client, "http://lake", {}, "quiet-sly-otter") is False


def test_is_slug_taken_parses_results_wrapper() -> None:
    """Delta-store's /deltas may return either a bare list or a dict with
    `results`/`deltas`. The helper must handle both — regression guard."""
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"results": [{"id": "x"}]}
    client.get.return_value = response

    assert is_slug_taken(client, "http://lake", {}, "s") is True


def test_generate_unique_slug_retries_on_collision() -> None:
    """Simulate one collision then a free slug on the second try."""
    client = MagicMock()
    taken_then_free = [
        MagicMock(status_code=200, **{"json.return_value": [{"id": "x"}]}),
        MagicMock(status_code=200, **{"json.return_value": []}),
    ]
    client.get.side_effect = taken_then_free

    slug = generate_unique_slug(client, "http://lake", {}, rng=random.Random(1))
    # Valid grammar returned on success — exercise matches the contract.
    assert slug.count("-") == 2
    assert client.get.call_count == 2


def test_generate_unique_slug_falls_back_with_suffix() -> None:
    """When every attempt is taken, the fallback appends a 3-letter suffix.
    Absurd in practice (10M combos vs max_attempts=10), but the safety net
    must actually work."""
    client = MagicMock()
    # Every attempt says "taken" — default 10 attempts + 1 fallback check? No:
    # fallback doesn't consult lake, just appends.
    response = MagicMock(status_code=200, **{"json.return_value": [{"id": "x"}]})
    client.get.return_value = response

    slug = generate_unique_slug(
        client, "http://lake", {}, max_attempts=3, rng=random.Random(1)
    )
    # Grammar: adj-adj-animal-suffix = four hyphen parts
    assert slug.count("-") == 3
    parts = slug.split("-")
    assert len(parts[-1]) == 3  # 3-letter suffix
