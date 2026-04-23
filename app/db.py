"""Database pool + tenant-scoped connection dependencies.

Three entry points into the pool, each guarded by transaction-scoped
session variables:

  * `get_db`           — normal tenant-scoped queries. Sets app.tenant_id
                         from request.state.tenant_id (populated by the
                         TenantResolutionMiddleware) + app.encryption_key
                         so pgcrypto helpers can resolve the key without
                         the caller having to remember to set it (CR-7).
                         RLS fires on every tenant-scoped table.
  * `get_db_bootstrap` — used by the middleware's api_keys lookup (before
                         any tenant is known). Sets app.bootstrap='true'
                         so the api_keys policy's OR branch lets the SELECT
                         through. Never exposed to route handlers.
  * `get_admin_db`     — used by the OAuth callback, where the tenant_id
                         arrives in the signed `state` param instead of via
                         an API key. Sets app.tenant_id + app.bootstrap +
                         app.encryption_key so pgcrypto-backed credential
                         writes succeed.

`get_pool` is kept for /readyz's bare SELECT 1 probe.

IMPORTANT: every SET LOCAL lives inside an explicit transaction. asyncpg's
`conn.transaction()` context manager commits on clean exit and rolls back
on exception — so a failing handler cannot leak tenant context into the
next request that reuses the connection.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException, Request, status

from app.config import get_settings
from app.logging import get_logger

if TYPE_CHECKING:
    import asyncpg
    from asyncpg.pool import Pool

log = get_logger(__name__)


async def set_encryption_key(conn: asyncpg.Connection) -> None:
    """Pin app.encryption_key for the current tx so pgcrypto helpers work.

    Kept as a public helper so every caller outside this module
    (e.g. `app/call_pipeline.py`) pins it the same way, and the
    eventual "rotate to a per-tenant key" refactor only touches one
    place. Expects to be called INSIDE an active tx.
    """
    settings = get_settings()
    await conn.execute(
        "SELECT set_config('app.encryption_key', $1, true)",
        settings.encryption_key,
    )


async def get_pool(request: Request) -> Pool:
    """Return the asyncpg pool attached to the FastAPI app state.

    Used by /readyz for a bare `SELECT 1` liveness probe. Not a full
    dependency — callers must open their own transaction if they intend
    to query tenant-scoped tables.
    """
    pool: Pool = request.app.state.db_pool
    return pool


async def get_db(request: Request) -> AsyncIterator[asyncpg.Connection]:
    """FastAPI dependency — tenant-scoped asyncpg connection.

    Acquires a connection, opens a transaction, sets `app.tenant_id` from
    `request.state.tenant_id` + `app.encryption_key` for pgcrypto, yields
    the connection to the route, and commits on clean exit (rolls back on
    exception).

    `request.state.tenant_id` MUST already be populated by the
    TenantResolutionMiddleware. If it isn't, something is architecturally
    wrong — we 500 rather than silently operating tenant-less.
    """
    tenant_id: UUID | None = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        log.error("get_db.missing_tenant_state", path=str(request.url.path))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="tenant context not established",
        )

    pool: Pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            # SET LOCAL — bound to this transaction only; auto-reverts.
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            await set_encryption_key(conn)
            yield conn


async def get_db_bootstrap(request: Request) -> AsyncIterator[asyncpg.Connection]:
    """Variant for the auth middleware's api_keys lookup.

    Sets `app.bootstrap='true'` inside a transaction so the (single)
    api_keys_tenant_isolation policy admits the lookup without a tenant
    context. Used ONLY by the middleware — never exposed to routes.
    """
    pool: Pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.bootstrap', 'true', true)")
            yield conn


async def get_admin_db(
    request: Request, tenant_id: UUID
) -> AsyncIterator[asyncpg.Connection]:
    """OAuth-callback variant — accepts tenant_id explicitly.

    There's no API key on the OAuth callback (the caller is Salesforce
    redirecting the user's browser), so we can't rely on middleware to
    populate request.state.tenant_id. Instead the tenant_id is recovered
    from the signed `state` query param and threaded in explicitly here.

    We set app.tenant_id (so RLS admits the credentials write),
    app.bootstrap (so the api_keys SELECT policy admits the lookup if
    it happens in the same tx), and app.encryption_key so the
    `encrypt_refresh_token()` helper can find its key.
    """
    pool: Pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            await conn.execute("SELECT set_config('app.bootstrap', 'true', true)")
            await set_encryption_key(conn)
            yield conn
