"""Cross-tenant guard test.

A valid API key for tenant A, presented against a path containing
tenant B's UUID, must be rejected with HTTP 403.

Exercises the TenantResolutionMiddleware end-to-end over the FastAPI
app (via httpx ASGITransport) with a real Postgres + two real API keys.

Invocation:
    docker compose run --rm api pytest tests/test_tenant_mismatch.py -v
"""
from __future__ import annotations

from uuid import UUID

import pytest


@pytest.mark.asyncio
async def test_valid_key_wrong_tenant_returns_403(
    two_tenants_with_keys: tuple[UUID, UUID, str, str],
) -> None:
    """Key_A → path with tenant_B → 403."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app as fastapi_app

    tenant_a, tenant_b, key_a, _ = two_tenants_with_keys

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Key_A against path /admin/tenants/<tenant_B>/keys → 403.
        resp = await client.post(
            f"/admin/tenants/{tenant_b}/keys",
            headers={"X-API-Key": key_a},
            json={},
        )
    assert resp.status_code == 403, resp.text
    assert "tenant mismatch" in resp.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_missing_key_returns_401(
    two_tenants_with_keys: tuple[UUID, UUID, str, str],
) -> None:
    """No X-API-Key → 401 on admin paths."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app as fastapi_app

    tenant_a, _, _, _ = two_tenants_with_keys

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/admin/tenants/{tenant_a}/keys",
            json={},
        )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_valid_key_correct_tenant_returns_201(
    two_tenants_with_keys: tuple[UUID, UUID, str, str],
) -> None:
    """Key_A against path /admin/tenants/<tenant_A>/keys → 201 + new key returned."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app as fastapi_app

    tenant_a, _, key_a, _ = two_tenants_with_keys

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/admin/tenants/{tenant_a}/keys",
            headers={"X-API-Key": key_a},
            json={},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tenant_id"] == str(tenant_a)
    assert body["key"].startswith("reig_")
    assert resp.headers.get("X-REIG-Key-Warning") == "store-immediately"
