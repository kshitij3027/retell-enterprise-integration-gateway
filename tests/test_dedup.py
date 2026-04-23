"""Dedup + idempotency tests (CR-3 + CR-4 + CR-14).

These tests hit the live FastAPI app via httpx's ASGITransport and
assert the contract end-to-end:

  * First POST of a call_analyzed payload → 204 + X-REIG-Dedup-Status: miss
    + one processed_events row + one dedup.miss audit + one
    webhook.received.call_analyzed audit + process_call_analyzed invoked.
  * Subsequent replays (same body, same tenant) → 204 + miss→hit flip +
    audit shows the additional dedup.hit rows, no extra processed_events,
    no extra process_call_analyzed invocation.
  * 10 concurrent POSTs of the same body → exactly 1 miss + 9 hits +
    1 processed_events row + 1 downstream invocation — tests the
    ON CONFLICT race-safety.

Invocation:
    docker compose run --rm api pytest tests/test_dedup.py -v
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from tests.conftest import _admin_connect, read_audit_rows
from tests.fixtures.sign_fixture import sign_payload

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "valid_call_analyzed.json"


def _load_raw_body() -> bytes:
    """Load the valid call_analyzed fixture EXACTLY as bytes (HMAC-safe)."""
    return _FIXTURE_PATH.read_bytes()


async def _count_processed_events(tenant_id: UUID, call_id: str) -> int:
    """Count `processed_events` rows for the given (tenant, call). Admin role."""
    conn = await _admin_connect()
    try:
        count = await conn.fetchval(
            "SELECT count(*) FROM processed_events "
            "WHERE tenant_id = $1 AND call_id = $2",
            tenant_id,
            call_id,
        )
        # fetchval returns Any; coerce to int for strict typing / clarity.
        return int(count or 0)
    finally:
        await conn.close()


async def _wait_for_audit_count(
    tenant_id: UUID,
    event_type: str,
    expected: int,
    timeout_s: float = 3.0,
) -> list[dict[str, Any]]:
    """Poll audit_log until exactly `expected` rows with `event_type` appear.

    BackgroundTasks run AFTER the response is flushed, so tests need a
    small grace window. 3 s is well over any realistic CI round-trip.
    """
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
async def test_dedup_single_miss(one_tenant: UUID) -> None:
    """First valid call_analyzed POST → 204 miss + 1 processed_events row."""
    from app.main import app as fastapi_app

    body = _load_raw_body()
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

    # call_id from the fixture — referenced explicitly so the test self-documents.
    call_id = json.loads(body)["call"]["call_id"]

    # Exactly one processed_events row landed.
    assert await _count_processed_events(one_tenant, call_id) == 1

    # Audit rows for the miss AND the call_analyzed receipt.
    miss_rows = await _wait_for_audit_count(one_tenant, "dedup.miss", 1)
    assert miss_rows[0]["call_id"] == call_id

    recv_rows = await _wait_for_audit_count(
        one_tenant, "webhook.received.call_analyzed", 1
    )
    assert recv_rows[0]["call_id"] == call_id

    # No dedup.hit yet — this is the first POST.
    hit_rows = await read_audit_rows(tenant_id=one_tenant, event_type="dedup.hit")
    assert hit_rows == []


@pytest.mark.asyncio
async def test_dedup_five_replays(
    one_tenant: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST same payload 5×: 1 miss + 4 hits, 1 processed_events, 1 pipeline call."""
    from app import call_pipeline as pipeline_module
    from app.main import app as fastapi_app
    from app.routes import webhooks as webhooks_module

    # Count pipeline invocations — process_call_analyzed is patched at the
    # IMPORT SITE (webhooks_module), which is what BackgroundTasks sees.
    call_count = 0

    async def _counting_pipeline(*_args: Any, **_kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1

    monkeypatch.setattr(
        webhooks_module, "process_call_analyzed", _counting_pipeline
    )
    # Also patch the module-level function so any future import path sees
    # the counter (belt-and-braces; not strictly required in C4).
    monkeypatch.setattr(
        pipeline_module, "process_call_analyzed", _counting_pipeline
    )

    body = _load_raw_body()
    settings = get_settings()

    # Fresh signature per request (skew drift is irrelevant across a
    # handful of fast requests but keeps each POST self-contained).
    transport = ASGITransport(app=fastapi_app)
    dedup_statuses: list[str | None] = []
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(5):
            header = sign_payload(body, settings.retell_api_key)
            resp = await client.post(
                f"/webhooks/retell/{one_tenant}",
                content=body,
                headers={
                    "x-retell-signature": header,
                    "Content-Type": "application/json",
                },
            )
            assert resp.status_code == 204, resp.text
            dedup_statuses.append(resp.headers.get("X-REIG-Dedup-Status"))

    # First request must be miss; next four must be hits.
    assert dedup_statuses[0] == "miss", dedup_statuses
    assert dedup_statuses[1:] == ["hit"] * 4, dedup_statuses

    call_id = json.loads(body)["call"]["call_id"]

    # Single processed_events row — ON CONFLICT DO NOTHING held the line.
    assert await _count_processed_events(one_tenant, call_id) == 1

    # Audit shape: 1 dedup.miss, 4 dedup.hit, 1 webhook.received.call_analyzed.
    await _wait_for_audit_count(one_tenant, "dedup.miss", 1)
    await _wait_for_audit_count(one_tenant, "dedup.hit", 4)
    await _wait_for_audit_count(one_tenant, "webhook.received.call_analyzed", 1)

    # process_call_analyzed invoked exactly once (only the miss dispatches).
    # BackgroundTasks are scheduled before the response is returned, and
    # httpx's ASGITransport awaits them to completion before returning —
    # so by the time the loop exits all 5 requests' tasks have run.
    assert call_count == 1, f"expected exactly 1 pipeline call, got {call_count}"


@pytest.mark.asyncio
async def test_dedup_concurrent(
    one_tenant: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10 coroutines POST same body concurrently → 1 miss + 9 hits, 1 pipeline call."""
    from app import call_pipeline as pipeline_module
    from app.main import app as fastapi_app
    from app.routes import webhooks as webhooks_module

    call_count = 0

    async def _counting_pipeline(*_args: Any, **_kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1

    monkeypatch.setattr(
        webhooks_module, "process_call_analyzed", _counting_pipeline
    )
    monkeypatch.setattr(
        pipeline_module, "process_call_analyzed", _counting_pipeline
    )

    body = _load_raw_body()
    settings = get_settings()

    # Pre-compute ONE signature and reuse across all concurrent POSTs —
    # Retell's signature is a function of (body, ts), so identical bytes
    # + identical ts = identical sig. That's exactly the race we want to
    # exercise: the real world can replay the same signed body in a burst.
    header = sign_payload(body, settings.retell_api_key)

    transport = ASGITransport(app=fastapi_app)

    async def _one_post(client: AsyncClient) -> str | None:
        resp = await client.post(
            f"/webhooks/retell/{one_tenant}",
            content=body,
            headers={
                "x-retell-signature": header,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 204, resp.text
        return resp.headers.get("X-REIG-Dedup-Status")

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        statuses = await asyncio.gather(*[_one_post(client) for _ in range(10)])

    # Exactly one miss, nine hits — in some order.
    miss_count = sum(1 for s in statuses if s == "miss")
    hit_count = sum(1 for s in statuses if s == "hit")
    assert miss_count == 1, f"expected exactly 1 miss in {statuses!r}"
    assert hit_count == 9, f"expected exactly 9 hits in {statuses!r}"

    call_id = json.loads(body)["call"]["call_id"]
    # Single processed_events row despite 10 concurrent inserters.
    assert await _count_processed_events(one_tenant, call_id) == 1

    # Audit shape: 1 miss, 9 hits. (Eventual-consistency window — the
    # BackgroundTasks finish on or before the outer ASGI scope exits.)
    await _wait_for_audit_count(one_tenant, "dedup.miss", 1)
    await _wait_for_audit_count(one_tenant, "dedup.hit", 9)
    await _wait_for_audit_count(one_tenant, "webhook.received.call_analyzed", 1)

    # Pipeline called exactly once.
    assert call_count == 1, f"expected exactly 1 pipeline call, got {call_count}"
