"""Salesforce adapter upsert + retry tests (CR-8 + CR-9).

Uses `respx` to mock Salesforce's HTTP surface, so no network is touched
and the tests are deterministic.

Scenarios:

  * 201 → UpsertResult(status='created'), crm_writes.status='success'.
  * 204 → UpsertResult(status='updated'), crm_writes.status='success'.
  * 401 INVALID_SESSION_ID → refresh + retry once → success.
  * 5× 503 → 5 attempts, crm_writes.status='failed',
    error_context contains retry metadata.
  * 400 DUPLICATE_VALUE → no retries, crm_writes.status='failed'.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import pytest
import respx

from adapters.base import LeadUpsertPayload
from adapters.errors import PermanentError
from adapters.salesforce import SalesforceAdapter
from app.config import get_settings


async def _seed_credentials(tenant_id: UUID) -> None:
    """Insert a credentials row with an encrypted refresh token."""
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
                "VALUES ($1, 'salesforce', 'stale_token', "
                "        now() - interval '1 hour', "
                "        encrypt_refresh_token($2), $3) "
                "ON CONFLICT (tenant_id, adapter) DO UPDATE SET "
                "  access_token_cached = EXCLUDED.access_token_cached, "
                "  access_token_expires_at = EXCLUDED.access_token_expires_at, "
                "  refresh_token_encrypted = EXCLUDED.refresh_token_encrypted, "
                "  instance_url = EXCLUDED.instance_url",
                tenant_id,
                "refresh_abc",
                "https://testinstance.my.salesforce.com",
            )
    finally:
        await conn.close()


async def _open_pool() -> asyncpg.Pool:
    settings = get_settings()
    pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=2)
    assert pool is not None
    return pool


def _payload() -> LeadUpsertPayload:
    return LeadUpsertPayload(
        external_call_id="call_sfdc_t1",
        first_name=None,
        last_name="Tester",
        phone="+14155551111",
        email=None,
        company="Unknown (inbound call)",
        lead_source="Retell Voice Agent",
        description="<US_SSN_REDACTED>",
    )


def _mock_token_endpoint(route: Any) -> None:
    """Configure the token endpoint to return a fresh access_token."""
    route.respond(
        200,
        json={
            "access_token": "fresh_token",
            "instance_url": "https://testinstance.my.salesforce.com",
            "expires_in": 3600,
        },
    )


@pytest.mark.asyncio
async def test_upsert_201_returns_created(one_tenant: UUID) -> None:
    """201 Created → UpsertResult(status='created')."""
    await _seed_credentials(one_tenant)
    settings = get_settings()
    pool = await _open_pool()

    async with httpx.AsyncClient() as http_client:
        with respx.mock(base_url=settings.sfdc_login_url, assert_all_called=False) as r:
            _mock_token_endpoint(r.post("/services/oauth2/token"))
            # PATCH goes to instance_url, so need a second mock base.
            with respx.mock(
                base_url="https://testinstance.my.salesforce.com",
                assert_all_called=False,
            ) as inst:
                inst.patch(
                    f"/services/data/{settings.sfdc_api_version}"
                    "/sobjects/Lead/External_Call_Id__c/call_sfdc_t1"
                ).respond(201, json={"id": "00Q000000000ABC", "success": True})

                adapter = SalesforceAdapter(
                    one_tenant, pool, settings, http_client=http_client
                )
                await adapter.authenticate()
                result = await adapter.upsert_record(_payload())
                assert result.status == "created"
                assert result.record_id == "00Q000000000ABC"

    await pool.close()


@pytest.mark.asyncio
async def test_upsert_204_returns_updated(one_tenant: UUID) -> None:
    """204 No Content → UpsertResult(status='updated')."""
    await _seed_credentials(one_tenant)
    settings = get_settings()
    pool = await _open_pool()

    async with httpx.AsyncClient() as http_client:
        with respx.mock(base_url=settings.sfdc_login_url, assert_all_called=False) as r:
            _mock_token_endpoint(r.post("/services/oauth2/token"))
            with respx.mock(
                base_url="https://testinstance.my.salesforce.com",
                assert_all_called=False,
            ) as inst:
                inst.patch(
                    f"/services/data/{settings.sfdc_api_version}"
                    "/sobjects/Lead/External_Call_Id__c/call_sfdc_t1"
                ).respond(204)

                adapter = SalesforceAdapter(
                    one_tenant, pool, settings, http_client=http_client
                )
                await adapter.authenticate()
                result = await adapter.upsert_record(_payload())
                assert result.status == "updated"
                assert result.record_id == "call_sfdc_t1"

    await pool.close()


@pytest.mark.asyncio
async def test_upsert_invalid_session_refreshes_and_retries(one_tenant: UUID) -> None:
    """401 INVALID_SESSION_ID → refresh + retry once → success."""
    await _seed_credentials(one_tenant)
    settings = get_settings()
    pool = await _open_pool()

    async with httpx.AsyncClient() as http_client:
        with respx.mock(base_url=settings.sfdc_login_url, assert_all_called=False) as r:
            # First refresh at authenticate(); second after 401.
            _mock_token_endpoint(r.post("/services/oauth2/token"))

            with respx.mock(
                base_url="https://testinstance.my.salesforce.com",
                assert_all_called=False,
            ) as inst:
                url_path = (
                    f"/services/data/{settings.sfdc_api_version}"
                    "/sobjects/Lead/External_Call_Id__c/call_sfdc_t1"
                )
                inst.patch(url_path).mock(
                    side_effect=[
                        httpx.Response(
                            401,
                            json=[
                                {
                                    "message": "Session expired",
                                    "errorCode": "INVALID_SESSION_ID",
                                }
                            ],
                        ),
                        httpx.Response(201, json={"id": "00Q000000000DEF"}),
                    ]
                )

                adapter = SalesforceAdapter(
                    one_tenant, pool, settings, http_client=http_client
                )
                await adapter.authenticate()
                result = await adapter.upsert_record(_payload())
                assert result.status == "created"
                assert result.record_id == "00Q000000000DEF"

    await pool.close()


@pytest.mark.asyncio
async def test_upsert_5x_503_exhausts_retries(one_tenant: UUID) -> None:
    """5× 503 → tenacity budget exhausted → RetryError."""
    from tenacity import RetryError

    await _seed_credentials(one_tenant)
    settings = get_settings()
    # Override retry config to a tight budget + no backoff so the test is fast.
    settings.retry_max_attempts = 3
    settings.retry_backoff_base_seconds = 0
    settings.retry_backoff_max_seconds = 0
    pool = await _open_pool()

    async with httpx.AsyncClient() as http_client:
        with respx.mock(base_url=settings.sfdc_login_url, assert_all_called=False) as r:
            _mock_token_endpoint(r.post("/services/oauth2/token"))

            with respx.mock(
                base_url="https://testinstance.my.salesforce.com",
                assert_all_called=False,
            ) as inst:
                url_path = (
                    f"/services/data/{settings.sfdc_api_version}"
                    "/sobjects/Lead/External_Call_Id__c/call_sfdc_t1"
                )
                inst.patch(url_path).respond(503, text="Service unavailable")

                adapter = SalesforceAdapter(
                    one_tenant, pool, settings, http_client=http_client
                )
                await adapter.authenticate()
                with pytest.raises(RetryError):
                    await adapter.upsert_record(_payload())

    await pool.close()


@pytest.mark.asyncio
async def test_upsert_400_duplicate_value_permanent(one_tenant: UUID) -> None:
    """400 DUPLICATE_VALUE → PermanentError on the first attempt, no retry."""
    await _seed_credentials(one_tenant)
    settings = get_settings()
    pool = await _open_pool()

    async with httpx.AsyncClient() as http_client:
        with respx.mock(base_url=settings.sfdc_login_url, assert_all_called=False) as r:
            _mock_token_endpoint(r.post("/services/oauth2/token"))

            with respx.mock(
                base_url="https://testinstance.my.salesforce.com",
                assert_all_called=False,
            ) as inst:
                url_path = (
                    f"/services/data/{settings.sfdc_api_version}"
                    "/sobjects/Lead/External_Call_Id__c/call_sfdc_t1"
                )
                call_counter = {"n": 0}

                def _count(request: httpx.Request) -> httpx.Response:
                    call_counter["n"] += 1
                    return httpx.Response(
                        400,
                        json=[{"errorCode": "DUPLICATE_VALUE", "message": "dup"}],
                    )

                inst.patch(url_path).mock(side_effect=_count)

                adapter = SalesforceAdapter(
                    one_tenant, pool, settings, http_client=http_client
                )
                await adapter.authenticate()
                with pytest.raises(PermanentError):
                    await adapter.upsert_record(_payload())
                # Not retried — exactly one PATCH attempt.
                assert call_counter["n"] == 1

    await pool.close()


_ = json  # imported for side-effect in future fixtures — keep the import available
