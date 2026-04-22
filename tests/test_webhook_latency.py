"""Webhook latency test (CR-1 — 204 within 2 s).

Fires 10 valid-signed webhooks at `POST /webhooks/retell/{tenant_id}`
and asserts every response came back in under `webhook_response_sla_seconds`
(default 2 s). Each request is measured synchronously around the httpx
call — NOT including any background audit-write time.

We can't easily hook structlog output from inside the test, so the
"no latency_exceeded WARN" assertion is done indirectly: if every
elapsed < SLA, then the route handler's `log.warning` branch would not
have fired.

Invocation:
    docker compose run --rm api pytest tests/test_webhook_latency.py -v
"""
from __future__ import annotations

import time
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from tests.fixtures.sign_fixture import sign_payload

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "valid_call_analyzed.json"


@pytest.mark.asyncio
async def test_webhook_latency_under_sla(one_tenant: UUID) -> None:
    """Fire 10 signed webhooks; assert max elapsed < SLA."""
    from app.main import app as fastapi_app

    body = _FIXTURE_PATH.read_bytes()
    settings = get_settings()
    # Generate one fresh signature per request so each is independently
    # valid (skew drift never matters across 10 fast requests, but doing
    # this keeps each request standalone).
    sla_ms = settings.webhook_response_sla_seconds * 1000

    transport = ASGITransport(app=fastapi_app)
    elapsed_ms_list: list[float] = []
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(10):
            header = sign_payload(body, settings.retell_api_key)
            start = time.perf_counter()
            resp = await client.post(
                f"/webhooks/retell/{one_tenant}",
                content=body,
                headers={
                    "x-retell-signature": header,
                    "Content-Type": "application/json",
                },
            )
            dur_ms = (time.perf_counter() - start) * 1000
            elapsed_ms_list.append(dur_ms)
            assert resp.status_code == 204, resp.text

    max_ms = max(elapsed_ms_list)
    assert max_ms < sla_ms, (
        f"max webhook latency {max_ms:.1f}ms exceeded SLA {sla_ms}ms; "
        f"all={elapsed_ms_list!r}"
    )
