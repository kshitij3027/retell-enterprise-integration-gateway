"""Shared pytest fixtures.

C1 only needs the app import + a stub DB pool so /readyz tests don't need a
live Postgres. Full DB-backed fixtures land in C2's tests/test_rls.py where
they're actually exercised.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

# Make sure the required env vars exist before importing the app.
# (Without these, `get_settings()` will raise on the first import.)
os.environ.setdefault("REIG_DATABASE_URL", "postgresql://reig:reig@localhost:5432/reig")
os.environ.setdefault("REIG_ENCRYPTION_KEY", "test_only_not_a_real_key_0000000000")
os.environ.setdefault("REIG_RETELL_API_KEY", "test_retell_placeholder")
os.environ.setdefault("REIG_SFDC_CLIENT_ID", "test_sfdc_client_placeholder")
os.environ.setdefault("REIG_SFDC_CLIENT_SECRET", "test_sfdc_secret_placeholder")


class _StubConnection:
    """Minimal asyncpg-compatible connection stub returning 1 from SELECT 1."""

    async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> Any:
        if query.strip().upper().startswith("SELECT 1"):
            return 1
        return None


class _StubAcquire:
    """Async context manager that yields a _StubConnection."""

    async def __aenter__(self) -> _StubConnection:
        return _StubConnection()

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


class _StubPool:
    """Minimal asyncpg.Pool stand-in for tests that don't hit real SQL."""

    def acquire(self) -> _StubAcquire:
        return _StubAcquire()

    async def close(self) -> None:
        return None


@pytest.fixture
async def app_with_stub_pool() -> AsyncIterator[Any]:
    """Import the FastAPI app, skip its real lifespan, inject a stub pool."""
    from app.main import app as fastapi_app

    # Replace the asyncpg pool with the stub. We bypass the lifespan
    # entirely — AsyncClient(transport=ASGITransport(...)) doesn't run it.
    fastapi_app.state.db_pool = _StubPool()
    yield fastapi_app
