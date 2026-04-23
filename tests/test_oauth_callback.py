"""OAuth callback tests (CR-7 + CR-8).

Verifies that the `GET /admin/oauth/callback` route:
  1. Rejects a bad state signature with 400.
  2. Accepts a valid signed state, exchanges the code via mocked SFDC,
     and persists `{access_token, refresh_token_encrypted, instance_url}`
     into `credentials`.
  3. Round-trips decrypt_refresh_token → the plaintext we mocked.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.routes.oauth import _sign_state


async def _fetch_credentials(tenant_id: UUID) -> dict[str, Any] | None:
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.admin_database_url)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            await conn.execute(
                "SELECT set_config('app.encryption_key', $1, true)",
                settings.encryption_key,
            )
            row = await conn.fetchrow(
                "SELECT access_token_cached, instance_url, "
                "       decrypt_refresh_token(refresh_token_encrypted) AS refresh "
                "FROM credentials WHERE tenant_id = $1 AND adapter = 'salesforce'",
                tenant_id,
            )
            return dict(row) if row is not None else None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_callback_rejects_invalid_state(one_tenant: UUID) -> None:
    """Tampered state → 400 bad request; no DB writes."""
    from app.main import app as fastapi_app

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/oauth/callback",
            params={"code": "c1", "state": "bogus.parts.ts.digest"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_persists_encrypted_refresh_token(one_tenant: UUID) -> None:
    """Valid state + successful token exchange → credentials row written."""
    from app.main import app as fastapi_app

    settings = get_settings()
    state = _sign_state(one_tenant, "nonce-xyz", settings.encryption_key)

    # Mock the SFDC token exchange.
    with respx.mock(
        base_url=settings.sfdc_login_url, assert_all_called=False
    ) as mocker:
        mocker.post("/services/oauth2/token").respond(
            200,
            json={
                "access_token": "access_abc",
                "refresh_token": "refresh_abc",
                "instance_url": "https://testinstance.my.salesforce.com",
                "expires_in": 3600,
            },
        )

        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/admin/oauth/callback",
                params={"code": "oauth_code_abc", "state": state},
            )
    assert resp.status_code == 200, resp.text

    creds = await _fetch_credentials(one_tenant)
    assert creds is not None
    assert creds["access_token_cached"] == "access_abc"
    assert creds["instance_url"] == "https://testinstance.my.salesforce.com"
    # Round-trip via decrypt_refresh_token helper (runs inside _fetch_credentials).
    assert creds["refresh"] == "refresh_abc"


# Unused import kept to document the test's httpx dependency.
_ = httpx
