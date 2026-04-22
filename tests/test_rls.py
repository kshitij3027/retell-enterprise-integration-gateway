"""RLS isolation test — T_A's connection can only see T_A's rows.

Two tenants A and B each insert a row into `calls` via their own RLS
transaction (SET LOCAL app.tenant_id = <their_id>). When A's connection
SELECTs without a WHERE clause, it must see exactly one row (its own).

Runs against the real Postgres in docker-compose — connects as `reig_app`
(the non-superuser role) so RLS actually fires.

Invocation:
    docker compose run --rm api pytest tests/test_rls.py -v
"""
from __future__ import annotations

from uuid import UUID

import pytest


@pytest.mark.asyncio
async def test_rls_isolates_tenants(two_tenants: tuple[UUID, UUID]) -> None:
    """T_A can only see its own calls rows; T_B can only see T_B's."""
    import asyncpg

    from app.config import get_settings

    tenant_a, tenant_b = two_tenants
    settings = get_settings()

    conn = await asyncpg.connect(dsn=settings.database_url)
    try:
        # Insert one call per tenant, each inside its own RLS tx.
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_a)
            )
            await conn.execute(
                "INSERT INTO calls (tenant_id, call_id) VALUES ($1, 'call_A')",
                tenant_a,
            )

        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_b)
            )
            await conn.execute(
                "INSERT INTO calls (tenant_id, call_id) VALUES ($1, 'call_B')",
                tenant_b,
            )

        # Now SELECT from T_A's context — must see 1 row, and that row
        # must be T_A's. No WHERE clause: the policy does the filtering.
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_a)
            )
            rows = await conn.fetch("SELECT tenant_id, call_id FROM calls")
        assert len(rows) == 1
        assert rows[0]["tenant_id"] == tenant_a
        assert rows[0]["call_id"] == "call_A"

        # Same from T_B's context.
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_b)
            )
            rows = await conn.fetch("SELECT tenant_id, call_id FROM calls")
        assert len(rows) == 1
        assert rows[0]["tenant_id"] == tenant_b
        assert rows[0]["call_id"] == "call_B"
    finally:
        await conn.close()
