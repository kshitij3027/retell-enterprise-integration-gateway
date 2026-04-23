"""Cross-tenant isolation tests (SC-4 / CR-5).

Two tenants with their own API keys. Calling `/admin/tenants/<B>/...`
with A's key must 403 — the middleware's path-param check catches it.
As a deeper belt-and-braces assertion, we also verify that at the SQL
layer (bypassing the middleware) a session pinned to tenant A sees
zero rows when selecting with `WHERE tenant_id = B`.
"""
from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings


@pytest.mark.asyncio
async def test_cross_tenant_http_403(
    two_tenants_with_keys: tuple[UUID, UUID, str, str],
) -> None:
    """A's key against /admin/tenants/B/... → 403."""
    from app.main import app as fastapi_app

    tenant_a, tenant_b, key_a, _key_b = two_tenants_with_keys
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/admin/tenants/{tenant_b}/calls",
            headers={"X-API-Key": key_a},
        )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_rls_blocks_at_db_layer(
    two_tenants_with_keys: tuple[UUID, UUID, str, str],
) -> None:
    """Even with SET LOCAL app.tenant_id=A, B's rows are invisible.

    Seeds a calls row for each tenant via the admin role, then reads as
    the runtime `reig_app` role with A's tenant pinned. Only A's row
    should be visible.
    """
    tenant_a, tenant_b, _key_a, _key_b = two_tenants_with_keys
    settings = get_settings()

    # Seed as admin so RLS doesn't block the insert.
    admin = await asyncpg.connect(dsn=settings.admin_database_url)
    try:
        async with admin.transaction():
            await admin.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_a)
            )
            await admin.execute(
                "INSERT INTO calls (tenant_id, call_id) VALUES ($1, $2)",
                tenant_a,
                "call_A",
            )
        async with admin.transaction():
            await admin.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_b)
            )
            await admin.execute(
                "INSERT INTO calls (tenant_id, call_id) VALUES ($1, $2)",
                tenant_b,
                "call_B",
            )
    finally:
        await admin.close()

    # Read as reig_app role with tenant A pinned.
    runtime = await asyncpg.connect(dsn=settings.database_url)
    try:
        async with runtime.transaction():
            await runtime.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_a)
            )
            rows = await runtime.fetch("SELECT call_id FROM calls")
            call_ids = {r["call_id"] for r in rows}
        assert "call_A" in call_ids
        assert "call_B" not in call_ids
    finally:
        await runtime.close()
