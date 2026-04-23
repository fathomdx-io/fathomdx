"""Shared pytest fixtures for api/ tests.

Pattern: build an httpx.AsyncClient bound to the FastAPI app via ASGITransport
WITHOUT running the lifespan. The lifespan talks to the delta-store and starts
background tasks (chat_listener, auto_regen); tests should stay off that path
unless they explicitly opt in.

Integration tests that need the lifespan should define their own fixture that
wraps the client in `async with LifespanManager(app):` — don't bake that into
the default.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_env() -> None:
    # Keep the test process from accidentally reading developer env vars.
    # Unit tests assert on behavior with known-default settings.
    for key in list(os.environ):
        if key.startswith(("FATHOM_", "LLM_")):
            os.environ.pop(key, None)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    # Import inside the fixture so env isolation runs first.
    from api.server import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
