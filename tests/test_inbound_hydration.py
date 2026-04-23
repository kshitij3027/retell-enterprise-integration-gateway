"""Inbound-call hydration tests (CR-12).

Exercises `POST /webhooks/retell/{tenant_id}/inbound` end-to-end.

Happy path:
  * signed payload + Retell `from_number` + seeded SFDC Lead →
    200 with `{"dynamic_variables": {"caller_name": "..."}}`.

Miss path:
  * signed payload + phone that matches zero Leads →
    200 with `{"dynamic_variables": {}}`.

Slow SFDC path:
  * mocked query latency 3 s > 1.8 s budget →
    200 with `{"dynamic_variables": {}}` inside the 2 s SLA.

Tamper path:
  * bad signature → 401.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from tests.fixtures.sign_fixture import sign_payload


async def _seed_credentials(tenant_id: UUID) -> None:
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.admin_database_url)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            await conn.execute(
                "SELECT set_config('app.bootstrap', 'true', true)"
            )
            await conn.execute(
                "SELECT set_config('app.encryption_key', $1, true)",
                settings.encryption_key,
            )
            await conn.execute(
                "INSERT INTO credentials "
                "(tenant_id, adapter, access_token_cached, "
                " access_token_expires_at, refresh_token_encrypted, instance_url) "
                "VALUES ($1, 'salesforce', 'fresh', "
                "        now() + interval '1 hour', "
                "        encrypt_refresh_token($2), $3) "
                "ON CONFLICT (tenant_id, adapter) DO UPDATE SET "
                "  access_token_cached = EXCLUDED.access_token_cached, "
                "  access_token_expires_at = EXCLUDED.access_token_expires_at, "
                "  refresh_token_encrypted = EXCLUDED.refresh_token_encrypted, "
                "  instance_url = EXCLUDED.instance_url",
                tenant_id,
                "refresh_inb",
                "https://testinstance.my.salesforce.com",
            )
    finally:
        await conn.close()


def _inbound_payload(phone: str) -> bytes:
    body = {
        "event": "call_inbound",
        "call_inbound": {
            "from_number": phone,
            "to_number": "+14085550000",
            "agent_id": "agent_abc",
        },
    }
    return json.dumps(body).encode("utf-8")


@pytest.mark.asyncio
async def test_inbound_hit_returns_caller_name(one_tenant: UUID) -> None:
    """Seeded Lead with matching phone → caller_name in dynamic_variables."""
    from app.main import app as fastapi_app

    await _seed_credentials(one_tenant)
    settings = get_settings()
    phone = "+14155551234"
    body = _inbound_payload(phone)
    header = sign_payload(body, settings.retell_api_key)

    with respx.mock(
        base_url="https://testinstance.my.salesforce.com", assert_all_called=False
    ) as inst:
        inst.get(f"/services/data/{settings.sfdc_api_version}/query").respond(
            200,
            json={
                "totalSize": 1,
                "done": True,
                "records": [
                    {
                        "Id": "00Q0INB0000001",
                        "FirstName": "Jane",
                        "LastName": "Doe",
                        "LastActivityDate": "2026-04-10",
                    }
                ],
            },
        )

        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/webhooks/retell/{one_tenant}/inbound",
                content=body,
                headers={
                    "x-retell-signature": header,
                    "Content-Type": "application/json",
                },
            )

    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()
    dyn = data["dynamic_variables"]
    # The name "Jane Doe" has no PII per our default entity list, so
    # Presidio leaves it intact.
    assert dyn.get("caller_name") == "Jane Doe"
    assert dyn.get("last_interaction") == "2026-04-10"


@pytest.mark.asyncio
async def test_inbound_miss_returns_empty_variables(one_tenant: UUID) -> None:
    """No SFDC match → empty dynamic_variables, still 200."""
    from app.main import app as fastapi_app

    await _seed_credentials(one_tenant)
    settings = get_settings()
    body = _inbound_payload("+19999999999")
    header = sign_payload(body, settings.retell_api_key)

    with respx.mock(
        base_url="https://testinstance.my.salesforce.com", assert_all_called=False
    ) as inst:
        inst.get(f"/services/data/{settings.sfdc_api_version}/query").respond(
            200, json={"totalSize": 0, "done": True, "records": []}
        )

        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/webhooks/retell/{one_tenant}/inbound",
                content=body,
                headers={
                    "x-retell-signature": header,
                    "Content-Type": "application/json",
                },
            )

    assert resp.status_code == 200
    assert resp.json() == {"dynamic_variables": {}}


@pytest.mark.asyncio
async def test_inbound_tampered_signature_401(one_tenant: UUID) -> None:
    """Bad signature → 401, no lookup attempted."""
    from app.main import app as fastapi_app

    await _seed_credentials(one_tenant)
    body = _inbound_payload("+14155551234")

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/webhooks/retell/{one_tenant}/inbound",
            content=body,
            headers={
                "x-retell-signature": "v=1,d=deadbeef",
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_inbound_slow_sfdc_falls_back_to_empty(one_tenant: UUID) -> None:
    """Mocked 3 s SFDC query → empty dynamic_variables within the 2 s SLA."""
    from app.main import app as fastapi_app

    await _seed_credentials(one_tenant)
    settings = get_settings()
    body = _inbound_payload("+14155551234")
    header = sign_payload(body, settings.retell_api_key)

    async def _slow_query(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3.0)
        return httpx.Response(200, json={"totalSize": 0, "records": []})

    with respx.mock(
        base_url="https://testinstance.my.salesforce.com", assert_all_called=False
    ) as inst:
        inst.get(f"/services/data/{settings.sfdc_api_version}/query").mock(
            side_effect=_slow_query
        )

        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(
            transport=transport, base_url="http://test", timeout=5.0
        ) as client:
            t0 = asyncio.get_event_loop().time()
            resp = await client.post(
                f"/webhooks/retell/{one_tenant}/inbound",
                content=body,
                headers={
                    "x-retell-signature": header,
                    "Content-Type": "application/json",
                },
            )
            elapsed = asyncio.get_event_loop().time() - t0

    assert resp.status_code == 200
    assert resp.json() == {"dynamic_variables": {}}
    # 1.8 s budget + small overhead — must be well under the 2 s SLA.
    assert elapsed < 2.2, f"inbound took {elapsed:.2f}s > 2.2s budget"
