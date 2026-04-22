"""End-to-end webhook signature tests (CR-2).

Exercises `POST /webhooks/retell/{tenant_id}` with:
  * valid signed body      → 204
  * tampered body          → 401 + audit row
  * wrong header (no match)→ 401 + audit row
  * stale timestamp (400s) → 401 + audit row + reason="timestamp_skew"
  * missing header         → 401 + audit row + reason="missing_header"
  * malformed header       → 401 + audit row + reason="malformed_header"

Runs against the real Postgres + FastAPI app (httpx ASGITransport).
Requires the `one_tenant` fixture for a concrete path-param tenant_id.

Invocation:
    docker compose run --rm api pytest tests/test_signature.py -v
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.signature import verify_retell_signature
from tests.conftest import read_audit_rows
from tests.fixtures.sign_fixture import sign_payload

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "valid_call_analyzed.json"


def _load_raw_body() -> bytes:
    """Load the test payload exactly as bytes — do NOT round-trip through dict."""
    return _FIXTURE_PATH.read_bytes()


# ---------------------------------------------------------------------------
# Unit tests for verify_retell_signature — no HTTP, no DB.
# ---------------------------------------------------------------------------
def test_verify_accepts_fresh_signed_body() -> None:
    body = b'{"event":"call_analyzed"}'
    api_key = "test_secret_xyz"
    header = sign_payload(body, api_key)
    result = verify_retell_signature(body, header, api_key)
    assert result.is_valid is True
    assert result.reason is None
    assert result.timestamp is not None


def test_verify_rejects_tampered_body() -> None:
    body = b'{"event":"call_analyzed"}'
    api_key = "test_secret_xyz"
    header = sign_payload(body, api_key)
    tampered = body + b" "
    result = verify_retell_signature(tampered, header, api_key)
    assert result.is_valid is False
    assert result.reason == "signature_mismatch"


def test_verify_rejects_missing_header() -> None:
    result = verify_retell_signature(b"{}", None, "k")
    assert result.is_valid is False
    assert result.reason == "missing_header"
    assert result.timestamp is None


def test_verify_rejects_empty_header() -> None:
    result = verify_retell_signature(b"{}", "", "k")
    assert result.is_valid is False
    assert result.reason == "missing_header"


def test_verify_rejects_malformed_header() -> None:
    result = verify_retell_signature(b"{}", "v=abc,d=xxx", "k")
    assert result.is_valid is False
    assert result.reason == "malformed_header"


def test_verify_rejects_stale_timestamp() -> None:
    body = b'{"event":"call_analyzed"}'
    api_key = "test_secret_xyz"
    stale_ms = int(time.time() * 1000) - 400_000  # 400 s ago
    header = sign_payload(body, api_key, ts=stale_ms)
    result = verify_retell_signature(body, header, api_key, skew_seconds=300)
    assert result.is_valid is False
    assert result.reason == "timestamp_skew"
    assert result.timestamp == stale_ms


def test_verify_sdk_roundtrip() -> None:
    """Our locally-generated sig must be accepted by the upstream Retell SDK.

    This is the "did I match the algorithm" sanity check — if this test
    fails, `sign_fixture.py` has drifted from the SDK's expectation.
    """
    from retell.lib.webhook_auth import verify as sdk_verify

    body = b'{"event":"call_analyzed","call":{"call_id":"abc"}}'
    api_key = "test_sdk_roundtrip_secret"
    header = sign_payload(body, api_key)
    # SDK's verify takes the body as a str (decoded), the api_key, and the header.
    assert sdk_verify(body.decode("utf-8"), api_key, header) is True


# ---------------------------------------------------------------------------
# HTTP-level tests — live FastAPI app + live Postgres.
# ---------------------------------------------------------------------------
async def _wait_for_audit_row(
    tenant_id: UUID,
    event_type: str,
    expected_reason: str,
    timeout_s: float = 2.0,
) -> dict[str, Any]:
    """Poll audit_log until a matching row appears (BackgroundTask race)."""
    deadline = time.monotonic() + timeout_s
    last_rows: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        rows = await read_audit_rows(tenant_id=tenant_id, event_type=event_type)
        last_rows = rows
        for row in rows:
            payload = row["payload"]
            # asyncpg returns jsonb as parsed dict — but if it comes back
            # as str (older driver), decode it here.
            if isinstance(payload, str):
                payload = json.loads(payload)
            if payload.get("reason") == expected_reason:
                # Return the row with payload normalised.
                row["payload"] = payload
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"no audit row with event_type={event_type} reason={expected_reason} "
        f"appeared within {timeout_s}s; last_rows={last_rows!r}"
    )


@pytest.mark.asyncio
async def test_valid_signature_returns_204(one_tenant: UUID) -> None:
    """Valid signed body → 204, no audit row."""
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
    assert resp.headers.get("X-REIG-Trace-Id") == "0"
    assert resp.headers.get("X-REIG-Dedup-Status") == "miss"

    # Give any (unexpected) background task a moment, then assert no
    # signature.failed row landed.
    await asyncio.sleep(0.1)
    rows = await read_audit_rows(
        tenant_id=one_tenant, event_type="signature.failed"
    )
    assert rows == [], f"did not expect a signature.failed row; got {rows!r}"


@pytest.mark.asyncio
async def test_tampered_body_returns_401_and_audits(one_tenant: UUID) -> None:
    """Tampered body → 401 + audit row with reason='signature_mismatch'."""
    from app.main import app as fastapi_app

    body = _load_raw_body()
    settings = get_settings()
    header = sign_payload(body, settings.retell_api_key)
    tampered = body + b" "

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/webhooks/retell/{one_tenant}",
            content=tampered,
            headers={
                "x-retell-signature": header,
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 401

    row = await _wait_for_audit_row(
        one_tenant, "signature.failed", "signature_mismatch"
    )
    assert row["actor"] == "fastapi:webhook_receiver"
    assert row["payload"]["header_present"] is True
    assert row["payload"]["body_length"] == len(tampered)
    # source_ip is the ASGI client (from httpx test transport) — either a
    # valid inet or None if not supplied. Both are acceptable; we only
    # assert the column exists on the returned row.
    assert "source_ip" in row


@pytest.mark.asyncio
async def test_missing_header_returns_401_and_audits(one_tenant: UUID) -> None:
    """No x-retell-signature header → 401 + audit row reason='missing_header'."""
    from app.main import app as fastapi_app

    body = _load_raw_body()

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/webhooks/retell/{one_tenant}",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 401

    row = await _wait_for_audit_row(
        one_tenant, "signature.failed", "missing_header"
    )
    assert row["payload"]["header_present"] is False


@pytest.mark.asyncio
async def test_stale_timestamp_returns_401_and_audits(one_tenant: UUID) -> None:
    """Timestamp 400 s in the past → 401 + reason='timestamp_skew'."""
    from app.main import app as fastapi_app

    body = _load_raw_body()
    settings = get_settings()
    stale_ms = int(time.time() * 1000) - 400_000
    header = sign_payload(body, settings.retell_api_key, ts=stale_ms)

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
    assert resp.status_code == 401
    row = await _wait_for_audit_row(
        one_tenant, "signature.failed", "timestamp_skew"
    )
    assert row["payload"]["header_present"] is True


@pytest.mark.asyncio
async def test_wrong_header_returns_401_and_audits(one_tenant: UUID) -> None:
    """Valid-shape header signed by a different key → 401, reason='signature_mismatch'."""
    from app.main import app as fastapi_app

    body = _load_raw_body()
    # Sign with a DIFFERENT api_key than the one in settings.
    header = sign_payload(body, "this_is_not_the_real_key")

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
    assert resp.status_code == 401
    await _wait_for_audit_row(one_tenant, "signature.failed", "signature_mismatch")


@pytest.mark.asyncio
async def test_malformed_header_returns_401_and_audits(one_tenant: UUID) -> None:
    """Garbage in the header → 401, reason='malformed_header'."""
    from app.main import app as fastapi_app

    body = _load_raw_body()

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/webhooks/retell/{one_tenant}",
            content=body,
            headers={
                "x-retell-signature": "not-even-close-to-valid",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 401
    await _wait_for_audit_row(one_tenant, "signature.failed", "malformed_header")


@pytest.mark.asyncio
async def test_unknown_tenant_still_audits(db_pool: Any) -> None:
    """Claimed tenant that doesn't exist → 401 + audit row still lands.

    audit_log has no FK to tenants, so we CAN write a row even for a
    bogus claimed tenant — important so attacker probes land on the
    journal.
    """
    from app.main import app as fastapi_app

    body = _load_raw_body()
    bogus = uuid4()

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/webhooks/retell/{bogus}",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 401
    row = await _wait_for_audit_row(bogus, "signature.failed", "missing_header")
    assert row["tenant_id"] == bogus
