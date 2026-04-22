"""Shared pytest fixtures.

Two classes of fixture here:

1. Stub-pool fixtures (C1) — for /healthz and /readyz tests that don't
   need a live Postgres. The `app_with_stub_pool` fixture injects a minimal
   pool that answers SELECT 1 so ASGITransport tests run hermetically.

2. DB-backed fixtures (C2) — `two_tenants` and `two_tenants_with_keys`
   talk to the real Postgres behind docker-compose. They seed fresh tenant
   rows per test (and optionally an API key per tenant) and clean up
   afterwards. These require the REIG_DATABASE_URL env var to point at a
   reachable database, i.e. run inside `docker compose run --rm api`.

Role separation for DB-backed fixtures
--------------------------------------
`reig_app` (runtime role) has SELECT / INSERT / limited UPDATE — NO DELETE
on tenants / api_keys / calls / … That's on purpose: production code must
never need it. But tests DO need to seed + clean up rows deterministically.

So we open a **second, short-lived connection** as the schema owner
(`reig`) for seeding and teardown only. The app itself (and every
"what does the app see" assertion) keeps running against the
`reig_app`-scoped pool — that's the whole point of the RLS test.

The admin DSN is derived from `settings.admin_database_url` (see
`app/config.py`), which by default swaps the userinfo of `database_url`
from `reig_app:reig_app` → `reig:reig`.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest

# Make sure the required env vars exist before importing the app.
# (Without these, `get_settings()` will raise on the first import.)
os.environ.setdefault(
    "REIG_DATABASE_URL", "postgresql://reig_app:reig_app@db:5432/reig"
)
os.environ.setdefault("REIG_ENCRYPTION_KEY", "test_only_not_a_real_key_0000000000")
os.environ.setdefault("REIG_RETELL_API_KEY", "test_retell_placeholder")
os.environ.setdefault("REIG_SFDC_CLIENT_ID", "test_sfdc_client_placeholder")
os.environ.setdefault("REIG_SFDC_CLIENT_SECRET", "test_sfdc_secret_placeholder")


# ---------------------------------------------------------------------------
# Stub pool (kept from C1)
# ---------------------------------------------------------------------------
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

    fastapi_app.state.db_pool = _StubPool()
    yield fastapi_app


# ---------------------------------------------------------------------------
# DB-backed fixtures (C2+)
# ---------------------------------------------------------------------------
@pytest.fixture
async def db_pool() -> AsyncIterator[Any]:
    """Open an asyncpg pool as `reig_app` and attach it to the app.

    Scope: function. Each test gets a fresh pool. Not the most efficient
    but simplest for correctness — we only have a handful of DB-backed
    tests in C2. All app-facing reads/writes go through this pool so that
    RLS policies fire with the non-superuser role.
    """
    import asyncpg

    from app.config import get_settings
    from app.main import app as fastapi_app

    settings = get_settings()
    pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=1,
        max_size=4,
        server_settings={"application_name": "reig-tests"},
    )
    fastapi_app.state.db_pool = pool
    try:
        yield pool
    finally:
        await pool.close()


async def _admin_connect() -> Any:
    """Open a short-lived asyncpg connection as the schema-owner role.

    Used exclusively for seeding + teardown of tenants / api_keys / calls.
    Keeping this separate from the `reig_app` pool means the runtime role
    stays locked down (no DELETE grant) and tests still clean up cleanly.
    """
    import asyncpg

    from app.config import get_settings

    settings = get_settings()
    return await asyncpg.connect(dsn=settings.admin_database_url)


async def _insert_tenant(name: str, profile: str) -> UUID:
    """Insert one tenant row via the admin connection. Returns the new UUID."""
    conn = await _admin_connect()
    try:
        row = await conn.fetchrow(
            "INSERT INTO tenants (name, profile) VALUES ($1, $2) RETURNING id",
            name,
            profile,
        )
        assert row is not None
        new_id: UUID = row["id"]
        return new_id
    finally:
        await conn.close()


async def _cleanup_tenant(tenant_id: UUID) -> None:
    """Delete one tenant row (api_keys / calls cascade) via admin connection."""
    conn = await _admin_connect()
    try:
        await conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
    finally:
        await conn.close()


async def _seed_api_key(tenant_id: UUID, key_hash: str) -> None:
    """Insert one api_keys row via the admin connection.

    We still SET LOCAL app.tenant_id + app.bootstrap so the policy's
    WITH CHECK is satisfied even under the owner role — keeping the
    seed path identical to the CLI's bootstrap flow.
    """
    conn = await _admin_connect()
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            await conn.execute("SELECT set_config('app.bootstrap', 'true', true)")
            await conn.execute(
                "INSERT INTO api_keys (tenant_id, key_hash, key_prefix) "
                "VALUES ($1, $2, 'reig_')",
                tenant_id,
                key_hash,
            )
    finally:
        await conn.close()


@pytest.fixture
async def two_tenants(db_pool: Any) -> AsyncIterator[tuple[UUID, UUID]]:
    """Create two fresh tenants (T_A, T_B) for a test; delete them after.

    Seeding and teardown go through the admin connection. The `db_pool`
    fixture is still requested so app-facing assertions have a pool, but
    it is not used for the tenant-row writes.
    """
    tenant_a = await _insert_tenant("T_A", "consumer-lending")
    tenant_b = await _insert_tenant("T_B", "consumer-lending")
    try:
        yield tenant_a, tenant_b
    finally:
        await _cleanup_tenant(tenant_a)
        await _cleanup_tenant(tenant_b)


@pytest.fixture
async def one_tenant(db_pool: Any) -> AsyncIterator[UUID]:
    """Create one fresh tenant for the test; delete after.

    Shorter-form companion to `two_tenants` for tests that only exercise
    a single-tenant flow (e.g. webhook signature tests). Goes through
    the admin connection for seed + cleanup, same as the two-tenant
    fixture.
    """
    tenant_id = await _insert_tenant("T_webhook", "consumer-lending")
    try:
        yield tenant_id
    finally:
        await _cleanup_tenant(tenant_id)


async def read_audit_rows(
    tenant_id: UUID | None = None,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Read audit_log rows via the admin connection.

    Admin role bypasses RLS (owner role), so callers can scope by
    tenant_id explicitly if they want — useful for asserting that
    signature failures for a claimed tenant landed the expected row.

    Args:
        tenant_id: If non-None, filter to rows with this tenant_id.
        event_type: If non-None, filter to this event_type.

    Returns:
        List of dict-ified rows ordered created_at DESC.
    """
    conn = await _admin_connect()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            clauses.append(f"tenant_id = ${len(params)}")
        if event_type is not None:
            params.append(event_type)
            clauses.append(f"event_type = ${len(params)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await conn.fetch(
            "SELECT tenant_id, event_type, call_id, actor, payload, "
            "trace_id, source_ip, created_at "
            f"FROM audit_log{where} ORDER BY created_at DESC",
            *params,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@pytest.fixture
async def two_tenants_with_keys(
    db_pool: Any,
) -> AsyncIterator[tuple[UUID, UUID, str, str]]:
    """Two fresh tenants, each with one issued API key.

    Yields `(tenant_a_id, tenant_b_id, raw_key_a, raw_key_b)`. Cleanup
    deletes the tenants via the admin connection (api_keys cascade).
    """
    from app.auth import generate_key

    tenant_a = await _insert_tenant("T_A_keyed", "consumer-lending")
    tenant_b = await _insert_tenant("T_B_keyed", "consumer-lending")

    raw_a, hash_a = generate_key(prefix="reig_")
    raw_b, hash_b = generate_key(prefix="reig_")

    await _seed_api_key(tenant_a, hash_a)
    await _seed_api_key(tenant_b, hash_b)

    try:
        yield tenant_a, tenant_b, raw_a, raw_b
    finally:
        await _cleanup_tenant(tenant_a)
        await _cleanup_tenant(tenant_b)
