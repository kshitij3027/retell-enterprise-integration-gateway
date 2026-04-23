"""End-to-end test: webhook → redaction → calls.metadata (CR-10).

Fires a valid signed `call_analyzed` webhook carrying PII in the
transcript, waits for the BackgroundTask to run, and asserts that:

  * `calls.metadata->>'redacted_transcript'` contains the token
    `<US_SSN_REDACTED>` and `<CREDIT_CARD_REDACTED>`.
  * The same metadata contains NO raw SSN digits and NO raw credit
    card digits — defence-in-depth against a redactor regression.
  * `calls.metadata->>'pii_entities_removed'` > 0.

This is the one test that glues every previous commit's pieces
together (signature, dedup, routing, PII redaction). A regression in
any one of them fails here.
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
from tests.conftest import _admin_connect
from tests.fixtures.sign_fixture import sign_payload

_FIXTURE = Path(__file__).parent / "fixtures" / "valid_call_analyzed_with_pii.json"


async def _read_call_metadata(tenant_id: UUID, call_id: str) -> dict[str, Any] | None:
    """Fetch the calls.metadata jsonb for a (tenant, call). None if absent."""
    conn = await _admin_connect()
    try:
        row = await conn.fetchrow(
            "SELECT metadata FROM calls WHERE tenant_id = $1 AND call_id = $2",
            tenant_id,
            call_id,
        )
        if row is None:
            return None
        meta: Any = row["metadata"]
        # asyncpg hands back jsonb as either dict (new) or str (old); normalise.
        if isinstance(meta, str):
            return json.loads(meta)
        assert isinstance(meta, dict)
        return meta
    finally:
        await conn.close()


async def _wait_for_metadata(
    tenant_id: UUID, call_id: str, timeout_s: float = 5.0
) -> dict[str, Any]:
    """Poll until the BackgroundTask's upsert lands a row. 5 s is generous.

    BackgroundTasks run after the HTTP response is flushed. `redact()` on
    a short string with spaCy cold-start may take a second or two; the
    generous timeout avoids flaking CI.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        meta = await _read_call_metadata(tenant_id, call_id)
        if meta is not None and meta.get("redacted_transcript"):
            return meta
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"no calls.metadata for ({tenant_id}, {call_id}) within {timeout_s}s"
    )


@pytest.mark.asyncio
async def test_pipeline_redacts_transcript_end_to_end(one_tenant: UUID) -> None:
    """Full path: signed webhook → BackgroundTask → redacted row in calls."""
    from app.main import app as fastapi_app

    body = _FIXTURE.read_bytes()
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

    call_id = json.loads(body)["call"]["call_id"]
    meta = await _wait_for_metadata(one_tenant, call_id)

    redacted = meta["redacted_transcript"]
    assert isinstance(redacted, str)

    # Positive: redaction tokens are present for the entities we know
    # appear in the fixture (SSN + credit card).
    assert "<US_SSN_REDACTED>" in redacted
    assert "<CREDIT_CARD_REDACTED>" in redacted

    # Negative: the raw digit sequences must NOT appear. This is the
    # security-critical assertion — a Presidio regression that left
    # even a substring of the SSN in place would be a P0.
    assert "521-45-6789" not in redacted
    assert "4111 1111 1111 1111" not in redacted
    # Also assert on the email.
    assert "jane.doe@example.com" not in redacted

    # Counts aggregate.
    assert meta["pii_entities_removed"] >= 2
    counts = meta.get("pii_entity_counts", {})
    assert counts.get("US_SSN", 0) >= 1
    assert counts.get("CREDIT_CARD", 0) >= 1
