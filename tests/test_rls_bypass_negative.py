"""RLS default-deny test.

With `reig_app` (no BYPASSRLS) and NO SET LOCAL app.tenant_id, a bare
`SELECT * FROM calls` must return zero rows — the policy's USING clause
evaluates `current_setting('app.tenant_id', true)::uuid` to NULL, and
`tenant_id = NULL` is NULL (not TRUE), so nothing is admitted.

This proves that if the middleware or a handler forgets to pin
app.tenant_id, the app fails CLOSED rather than leaking.

Invocation:
    docker compose run --rm api pytest tests/test_rls_bypass_negative.py -v
"""
from __future__ import annotations

from uuid import UUID

import pytest


@pytest.mark.asyncio
async def test_unpinned_select_returns_zero_rows(
    two_tenants: tuple[UUID, UUID],
) -> None:
    """Without SET LOCAL, SELECT * FROM calls returns 0 rows."""
    import asyncpg

    from app.config import get_settings

    tenant_a, _ = two_tenants
    settings = get_settings()

    # First, insert one row as T_A so there IS data to leak if RLS fails.
    conn = await asyncpg.connect(dsn=settings.database_url)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_a)
            )
            await conn.execute(
                "INSERT INTO calls (tenant_id, call_id) VALUES ($1, 'call_seed')",
                tenant_a,
            )

        # Now, in a FRESH connection / tx with NO app.tenant_id set,
        # SELECT must see nothing.
        async with conn.transaction():
            # Explicitly reset in case something lingered on the connection.
            await conn.execute("RESET app.tenant_id")
            rows = await conn.fetch("SELECT tenant_id, call_id FROM calls")
        assert rows == []
    finally:
        await conn.close()
