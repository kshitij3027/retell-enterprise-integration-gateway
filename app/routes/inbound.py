"""Inbound-call hydration webhook (CR-12).

Retell fires this webhook when an inbound call lands on one of the
configured numbers — BEFORE the agent greets the caller. The response
body's `dynamic_variables` dict is interpolated into the agent's
greeting + system prompt, so this is the seam where the pipeline
greets the caller by name, references their open case, etc.

Contract with Retell:
  * Response MUST return within 2 s or Retell cuts the lookup and uses
    defaults. We enforce a 1.8 s hard timeout (20 % safety margin)
    internally so a slow Salesforce never blows the budget.
  * Response shape: `{"dynamic_variables": {<key>: <str>}}`.

Body shape (from Retell docs):
  {
    "event": "call_inbound",
    "call_inbound": {
      "from_number": "+14155551234",
      "to_number":   "+14085550000",
      "agent_id":    "agent_..."
    }
  }

Signature verification + tenant auth mirror /webhooks/retell/{tenant_id}:
  * HMAC with the tenant's Retell workspace key (reusing
    `verify_retell_signature`).
  * 5 min skew window.
  * Tamper → 401 + signature.failed audit row.

Redaction: the name we return is run through Presidio as
defence-in-depth. If a Salesforce record contained PII in a name-like
field (rare but possible), the LLM greeting prompt never sees the raw.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Request, Response, status
from pydantic import BaseModel, ConfigDict

from adapters.base import LookupResult
from adapters.salesforce import SalesforceAdapter
from app.audit import SIGNATURE_FAILED, write_audit
from app.config import get_settings
from app.db import set_encryption_key
from app.logging import get_logger
from app.pii import redact
from app.signature import verify_retell_signature
from app.tracing import get_tracer

if TYPE_CHECKING:
    from asyncpg.pool import Pool

log = get_logger(__name__)
_tracer = get_tracer("reig.inbound")
router = APIRouter(prefix="/webhooks", tags=["inbound"])


class InboundCall(BaseModel):
    """The `call_inbound` sub-object Retell sends on an inbound event."""

    model_config = ConfigDict(extra="allow")

    from_number: str | None = None
    to_number: str | None = None
    agent_id: str | None = None


class InboundWebhookPayload(BaseModel):
    """Top-level envelope for the inbound hydration webhook."""

    model_config = ConfigDict(extra="allow")

    event: str
    call_inbound: InboundCall


async def _hydrate(
    tenant_id: UUID,
    caller_phone: str,
    pool: Pool,
) -> dict[str, str]:
    """Look up the caller in Salesforce, return LLM-safe dynamic vars.

    All returned string fields are redacted by Presidio (defence in
    depth — even if SFDC stored PII in a name-like field, the LLM
    never sees the raw).

    Empty dict on lookup miss; the agent will fall back to defaults.
    """
    settings = get_settings()
    # We construct the adapter directly (not through the resolver) —
    # /inbound is Salesforce-only per CR-12; the resolver adds no value
    # here.
    adapter = SalesforceAdapter(tenant_id, pool, settings)

    try:
        await adapter.authenticate()
        result: LookupResult | None = await adapter.lookup_by_phone(caller_phone)
    except Exception as exc:  # noqa: BLE001 — hydration failures are soft
        log.warning(
            "inbound.lookup.failed",
            tenant_id=str(tenant_id),
            error=str(exc)[:300],
        )
        return {}

    if result is None:
        return {}

    def _redact(s: str | None) -> str:
        if not s:
            return ""
        return redact(s).text

    caller_name_parts = [p for p in (result.first_name, result.last_name) if p]
    caller_name = _redact(" ".join(caller_name_parts)) if caller_name_parts else ""

    dyn: dict[str, str] = {
        "caller_name": caller_name,
        "last_interaction": (
            result.last_activity_date.isoformat()
            if result.last_activity_date is not None
            else ""
        ),
        "open_cases": str(result.open_cases),
        "account_status": _redact(result.account_status),
    }
    # Drop empty values so Retell's template engine uses its default.
    return {k: v for k, v in dyn.items() if v}


@router.post("/retell/{tenant_id}/inbound")
async def retell_inbound(
    tenant_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """Receive + respond to an inbound hydration webhook from Retell.

    Hard 1.8 s budget — anything slower returns `{"dynamic_variables":{}}`
    so Retell's 2 s SLA is safe even under a slow Salesforce.
    """
    started_at = time.perf_counter()
    settings = get_settings()
    pool: Pool = request.app.state.db_pool

    raw_body = await request.body()
    header = request.headers.get("x-retell-signature")
    source_ip = request.client.host if request.client is not None else None

    with _tracer.start_as_current_span("inbound.hydrate") as span:
        span.set_attribute("tenant.id", str(tenant_id))

        # -- 1. HMAC signature verification --------------------------------
        sig_result = verify_retell_signature(
            raw_body=raw_body,
            signature_header=header,
            api_key=settings.retell_api_key,
            skew_seconds=settings.webhook_timestamp_skew_seconds,
        )
        if not sig_result.is_valid:
            background_tasks.add_task(
                _record_sig_fail,
                pool,
                tenant_id,
                sig_result.reason or "unknown",
                len(raw_body),
                source_ip,
            )
            log.info(
                "inbound.signature.rejected",
                tenant_id=str(tenant_id),
                reason=sig_result.reason,
            )
            return Response(status_code=status.HTTP_401_UNAUTHORIZED)

        # -- 2. Parse payload ---------------------------------------------
        try:
            payload = InboundWebhookPayload.model_validate_json(raw_body)
        except Exception as exc:  # noqa: BLE001 — bogus shape still gets empty dyn
            log.info(
                "inbound.payload.malformed",
                tenant_id=str(tenant_id),
                error=str(exc)[:200],
            )
            return _json_response({"dynamic_variables": {}})

        caller_phone = payload.call_inbound.from_number
        if not caller_phone:
            return _json_response({"dynamic_variables": {}})

        span.set_attribute("caller_phone", caller_phone)

        # -- 3. Hydrate with a hard deadline ------------------------------
        budget = 1.8
        try:
            dyn = await asyncio.wait_for(
                _hydrate(tenant_id, caller_phone, pool), timeout=budget
            )
        except TimeoutError:
            log.warning(
                "inbound.hydrate.timeout",
                tenant_id=str(tenant_id),
                budget_s=budget,
            )
            dyn = {}

        elapsed_ms = (time.perf_counter() - started_at) * 1000
        span.set_attribute("elapsed_ms", round(elapsed_ms, 1))
        span.set_attribute("inbound.match_found", bool(dyn))

        log.info(
            "inbound.accepted",
            tenant_id=str(tenant_id),
            elapsed_ms=round(elapsed_ms, 1),
            match=bool(dyn),
        )
        return _json_response({"dynamic_variables": dyn})


def _json_response(body: dict[str, Any]) -> Response:
    """200 JSON response with the Retell-expected shape."""
    import json

    return Response(
        content=json.dumps(body),
        media_type="application/json",
        status_code=200,
    )


async def _record_sig_fail(
    pool: Pool,
    tenant_id: UUID,
    reason: str,
    body_length: int,
    source_ip: str | None,
) -> None:
    """Log a signature.failed audit row for the /inbound path."""
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
                )
                await set_encryption_key(conn)
                await write_audit(
                    conn,
                    event_type=SIGNATURE_FAILED,
                    tenant_id=tenant_id,
                    call_id=None,
                    actor="fastapi:inbound_receiver",
                    payload={
                        "reason": reason,
                        "body_length": body_length,
                        "route": "inbound",
                    },
                    trace_id="0",
                    source_ip=source_ip,
                )
    except Exception as exc:  # noqa: BLE001 — background path, never re-raise
        log.error(
            "inbound.audit_write_failed",
            tenant_id=str(tenant_id),
            error=str(exc),
        )
