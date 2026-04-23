"""Event-type routing tests (CR-4 + CR-14).

`call_analyzed` is the only event that fires the downstream adapter
pipeline. `call_started` / `call_ended` land in the audit log but do NOT
call `process_call_analyzed`. Unknown events return 204 (never 500) and
get a `webhook.received.unknown` audit row. Malformed JSON returns 400
and lands a `webhook.received.malformed_json` audit row.

Invocation:
    docker compose run --rm api pytest tests/test_event_routing.py -v
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from tests.conftest import read_audit_rows
from tests.fixtures.sign_fixture import sign_payload


def _payload(event: str, call_id: str = "call_routing_001") -> bytes:
    """Build a minimal signable payload with the given event type."""
    body = {
        "event": event,
        "call": {
            "call_id": call_id,
            "from_number": "+14155551111",
            "to_number": "+14155552222",
        },
    }
    # NOTE: dump with keys in insertion order and no extra whitespace —
    # any drift here is fine because we sign the bytes we actually send
    # and nobody round-trips the dict on the server side.
    return json.dumps(body).encode("utf-8")


async def _wait_for_audit_count(
    tenant_id: UUID,
    event_type: str,
    expected: int,
    timeout_s: float = 3.0,
) -> list[dict[str, Any]]:
    """Poll audit_log until exactly `expected` rows appear."""
    deadline = time.monotonic() + timeout_s
    rows: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        rows = await read_audit_rows(tenant_id=tenant_id, event_type=event_type)
        if len(rows) == expected:
            return rows
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"expected {expected} audit rows with event_type={event_type!r} "
        f"for tenant {tenant_id}; got {len(rows)}: {rows!r}"
    )


@pytest.mark.asyncio
async def test_call_started_does_not_fire_pipeline(
    one_tenant: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`call_started` → 204, audit row, NO pipeline call."""
    from app import call_pipeline as pipeline_module
    from app.main import app as fastapi_app
    from app.routes import webhooks as webhooks_module

    # Raise on unexpected invocation — "did it fire?" gets an unambiguous answer.
    async def _must_not_fire(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(
            "process_call_analyzed must NOT fire on call_started"
        )

    monkeypatch.setattr(webhooks_module, "process_call_analyzed", _must_not_fire)
    monkeypatch.setattr(pipeline_module, "process_call_analyzed", _must_not_fire)

    body = _payload("call_started", call_id="call_started_routing")
    settings = get_settings()
    header = sign_payload(body, settings.retell_api_key)

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
    assert resp.headers.get("X-REIG-Dedup-Status") == "miss"

    await _wait_for_audit_count(
        one_tenant, "webhook.received.call_started", 1
    )
    # No call_analyzed audit row landed.
    assert (
        await read_audit_rows(
            tenant_id=one_tenant, event_type="webhook.received.call_analyzed"
        )
    ) == []


@pytest.mark.asyncio
async def test_call_ended_does_not_fire_pipeline(
    one_tenant: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`call_ended` → 204, audit row, NO pipeline call."""
    from app import call_pipeline as pipeline_module
    from app.main import app as fastapi_app
    from app.routes import webhooks as webhooks_module

    async def _must_not_fire(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(
            "process_call_analyzed must NOT fire on call_ended"
        )

    monkeypatch.setattr(webhooks_module, "process_call_analyzed", _must_not_fire)
    monkeypatch.setattr(pipeline_module, "process_call_analyzed", _must_not_fire)

    body = _payload("call_ended", call_id="call_ended_routing")
    settings = get_settings()
    header = sign_payload(body, settings.retell_api_key)

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
    await _wait_for_audit_count(one_tenant, "webhook.received.call_ended", 1)


@pytest.mark.asyncio
async def test_call_analyzed_fires_pipeline(
    one_tenant: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`call_analyzed` → 204 + pipeline called exactly once."""
    from app import call_pipeline as pipeline_module
    from app.main import app as fastapi_app
    from app.routes import webhooks as webhooks_module

    call_count = 0

    async def _counting_pipeline(*_args: Any, **_kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1

    monkeypatch.setattr(webhooks_module, "process_call_analyzed", _counting_pipeline)
    monkeypatch.setattr(pipeline_module, "process_call_analyzed", _counting_pipeline)

    body = _payload("call_analyzed", call_id="call_analyzed_routing")
    settings = get_settings()
    header = sign_payload(body, settings.retell_api_key)

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
    await _wait_for_audit_count(one_tenant, "webhook.received.call_analyzed", 1)
    assert call_count == 1, f"expected exactly 1 pipeline call, got {call_count}"


@pytest.mark.asyncio
async def test_unknown_event_is_204_not_500(one_tenant: UUID) -> None:
    """Unknown event string → 204 + webhook.received.unknown, no crash."""
    from app.main import app as fastapi_app

    # Deliberate: an event name Retell will never send. Must not 500.
    body = _payload("call_garbage", call_id="call_unknown_routing")
    settings = get_settings()
    header = sign_payload(body, settings.retell_api_key)

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
    rows = await _wait_for_audit_count(
        one_tenant, "webhook.received.unknown", 1
    )
    payload = rows[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload.get("event") == "call_garbage"


@pytest.mark.asyncio
async def test_malformed_json_returns_400(one_tenant: UUID) -> None:
    """Garbage bytes after a valid sig → 400 + webhook.received.malformed_json."""
    from app.main import app as fastapi_app

    body = b"this-is-not-json-at-all-{{"
    settings = get_settings()
    header = sign_payload(body, settings.retell_api_key)

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

    assert resp.status_code == 400, resp.text
    await _wait_for_audit_count(
        one_tenant, "webhook.received.malformed_json", 1
    )
