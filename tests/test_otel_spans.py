"""OpenTelemetry span-shape tests (CR-13).

Uses an in-memory span exporter to avoid a live Jaeger dependency. Fires
one signed `call_analyzed` webhook end-to-end with the Salesforce token +
upsert endpoints mocked via respx, then asserts:

  * webhook.received exists and carries retell.call_id + tenant.id.
  * signature.verified, dedup.checked, pii.redacted, audit.written,
    oauth.refreshed, adapter.upsert all appear as finished spans.
  * adapter.upsert carries sfdc.lead_id.

Not a perf test — just span-shape. The plan's final Jaeger UI assertion
lives in Final E2E F2.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.tracing import install_in_memory_tracer, reset_for_tests
from tests.fixtures.sign_fixture import sign_payload

_FIXTURE = Path(__file__).parent / "fixtures" / "valid_call_analyzed_with_pii.json"


async def _seed_credentials(tenant_id: UUID) -> None:
    """Seed a credentials row so authenticate() can find tokens."""
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
                "refresh_token_plain",
                "https://testinstance.my.salesforce.com",
            )
    finally:
        await conn.close()


@pytest.fixture
def in_memory_exporter() -> Any:
    """Install an in-memory span exporter; yield it; reset on teardown."""
    reset_for_tests()
    exporter = install_in_memory_tracer()
    yield exporter
    reset_for_tests()


@pytest.mark.asyncio
async def test_signed_webhook_emits_expected_span_shape(
    one_tenant: UUID, in_memory_exporter: Any
) -> None:
    """End-to-end: webhook → signature → dedup → pii → oauth → upsert → audit."""
    from app.main import app as fastapi_app

    settings = get_settings()
    await _seed_credentials(one_tenant)

    body = _FIXTURE.read_bytes()
    header = sign_payload(body, settings.retell_api_key)

    # Mock SFDC token + upsert endpoints.
    with respx.mock(base_url=settings.sfdc_login_url, assert_all_called=False) as r:
        r.post("/services/oauth2/token").respond(
            200,
            json={
                "access_token": "fresh_otel_token",
                "instance_url": "https://testinstance.my.salesforce.com",
                "expires_in": 3600,
            },
        )
        with respx.mock(
            base_url="https://testinstance.my.salesforce.com",
            assert_all_called=False,
        ) as inst:
            call_id = json.loads(body)["call"]["call_id"]
            inst.patch(
                f"/services/data/{settings.sfdc_api_version}"
                f"/sobjects/Lead/External_Call_Id__c/{call_id}"
            ).respond(201, json={"id": "00Q0SPAN0000001"})

            transport = ASGITransport(app=fastapi_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/webhooks/retell/{one_tenant}",
                    content=body,
                    headers={
                        "x-retell-signature": header,
                        "Content-Type": "application/json",
                    },
                )
            assert resp.status_code == 204, resp.text

    # BackgroundTasks run after the response; the ASGITransport flushes
    # them on client close. Let exporter get_finished_spans() settle.
    deadline = time.monotonic() + 5.0
    span_names: set[str] = set()
    while time.monotonic() < deadline:
        span_names = {s.name for s in in_memory_exporter.get_finished_spans()}
        if {
            "webhook.received",
            "signature.verified",
            "dedup.checked",
            "pii.redacted",
            "oauth.refreshed",
            "adapter.upsert",
            "audit.written",
        }.issubset(span_names):
            break
        await _async_sleep(0.1)

    assert "webhook.received" in span_names, span_names
    assert "signature.verified" in span_names, span_names
    assert "dedup.checked" in span_names, span_names
    assert "pii.redacted" in span_names, span_names
    assert "oauth.refreshed" in span_names, span_names
    assert "adapter.upsert" in span_names, span_names
    assert "audit.written" in span_names, span_names

    # Inspect attributes on specific spans.
    spans = in_memory_exporter.get_finished_spans()
    webhook = next(s for s in spans if s.name == "webhook.received")
    assert webhook.attributes.get("retell.call_id") == call_id
    assert webhook.attributes.get("tenant.id") == str(one_tenant)

    upsert = next(s for s in spans if s.name == "adapter.upsert")
    assert upsert.attributes.get("sfdc.lead_id") == "00Q0SPAN0000001"


async def _async_sleep(s: float) -> None:
    import asyncio

    await asyncio.sleep(s)


# Kept so the test file's httpx dep is visible to tooling.
_ = httpx
