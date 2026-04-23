"""adapter_resolver tests — tenant-scoped adapter dispatch (CR-11).

Exercises three code paths in `app/adapter_resolver.resolve_adapter`:

1. `active_adapter='salesforce'`   → SalesforceAdapter.
2. `active_adapter='servicenow_stub'` → ServiceNowAdapter.
3. `active_adapter=NULL` (or empty) → falls back to
   `settings.active_adapter`.

Uses the admin connection (same pattern as test_rls.py) to mutate
`tenants.active_adapter` directly — the runtime role has UPDATE on
`tenants`, but we keep consistent with the fixtures so teardown is
identical to every other DB-backed test.
"""
from __future__ import annotations

from uuid import UUID

import pytest

from adapters.salesforce import SalesforceAdapter
from adapters.servicenow import ServiceNowAdapter
from app.adapter_resolver import resolve_adapter
from app.config import get_settings
from tests.conftest import _admin_connect


async def _set_tenant_adapter(tenant_id: UUID, value: str | None) -> None:
    """Update tenants.active_adapter for a single tenant via the admin conn."""
    conn = await _admin_connect()
    try:
        await conn.execute(
            "UPDATE tenants SET active_adapter = $1 WHERE id = $2",
            value,
            tenant_id,
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_resolve_adapter_salesforce(one_tenant: UUID) -> None:
    """tenants.active_adapter='salesforce' → SalesforceAdapter instance."""
    await _set_tenant_adapter(one_tenant, "salesforce")

    settings = get_settings()
    conn = await _admin_connect()
    try:
        adapter = await resolve_adapter(one_tenant, conn, settings)
    finally:
        await conn.close()

    assert isinstance(adapter, SalesforceAdapter)


@pytest.mark.asyncio
async def test_resolve_adapter_servicenow(one_tenant: UUID) -> None:
    """tenants.active_adapter='servicenow_stub' → ServiceNowAdapter instance."""
    await _set_tenant_adapter(one_tenant, "servicenow_stub")

    settings = get_settings()
    conn = await _admin_connect()
    try:
        adapter = await resolve_adapter(one_tenant, conn, settings)
    finally:
        await conn.close()

    assert isinstance(adapter, ServiceNowAdapter)


@pytest.mark.asyncio
async def test_resolve_adapter_fallback_on_empty(one_tenant: UUID) -> None:
    """Empty-string adapter column → falls back to settings.active_adapter.

    The tenants.active_adapter column is NOT NULL DEFAULT 'salesforce',
    so we emulate the "unset" case by forcing an empty string. The
    resolver normalizes empty/whitespace to None and falls back.
    """
    # Column is NOT NULL, so set to empty string to simulate "unconfigured"
    await _set_tenant_adapter(one_tenant, "")

    settings = get_settings()
    # settings.active_adapter defaults to 'salesforce'
    assert settings.active_adapter == "salesforce"

    conn = await _admin_connect()
    try:
        adapter = await resolve_adapter(one_tenant, conn, settings)
    finally:
        await conn.close()

    assert isinstance(adapter, SalesforceAdapter)


@pytest.mark.asyncio
async def test_resolve_adapter_unknown_raises(one_tenant: UUID) -> None:
    """Mis-configured adapter name → ValueError (no silent fallback)."""
    await _set_tenant_adapter(one_tenant, "not_a_real_adapter")

    settings = get_settings()
    conn = await _admin_connect()
    try:
        with pytest.raises(ValueError, match="Unknown adapter"):
            await resolve_adapter(one_tenant, conn, settings)
    finally:
        await conn.close()
