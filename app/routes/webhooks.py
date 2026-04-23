"""Retell webhook receiver — signature + dedup + event routing (C3+C4).

CR-1: respond within 2 s. CR-2: HMAC verify, 5-min skew, 401 on any
failure, audit row written on fail. CR-3: idempotent claim on
`(tenant_id, call_id, event_type)`. CR-4: only `call_analyzed` fires the
downstream pipeline. CR-14: an audit row for every state transition.

Request lifecycle
-----------------
  1. Read raw bytes (BEFORE any json decode — re-encoding breaks HMAC).
  2. Verify HMAC. Fail => 401 + background `signature.failed` audit.
  3. Parse JSON via Pydantic. Fail => 400 + background
     `webhook.received.malformed_json` audit.
  4. Open a SHORT tenant-scoped tx; call `claim_event`.
     - hit  => 204 + background `dedup.hit` audit. Stop.
     - miss => 204 + background `dedup.miss` audit. Continue.
  5. Dispatch by `event`:
     - `call_started`  => background audit `webhook.received.call_started`.
     - `call_ended`    => background audit `webhook.received.call_ended`.
     - `call_analyzed` => background audit `webhook.received.call_analyzed`
                         AND background `process_call_analyzed(...)`.
     - anything else   => background audit `webhook.received.unknown`.
  6. Return 204 with `X-REIG-Dedup-Status: <hit|miss>`.

Why this route is exempt from TenantResolutionMiddleware
--------------------------------------------------------
Retell signs the body with the tenant's workspace API key (HMAC). The
signature IS the authentication — there's no `X-API-Key` header on
these requests. The middleware's exemption list already covers
`/webhooks/retell/` (see app/middleware/tenant.py `_EXEMPT_PREFIXES`).

Why audit writes bypass `get_db`
--------------------------------
`get_db` requires `request.state.tenant_id` — which the middleware
skipped setting because this path is exempt. We acquire directly from
the pool, open our own short-lived transaction, and set
`app.tenant_id = <claimed path-param>` so tenant-RLS-guarded INSERTs
pass their WITH CHECK. The claimed tenant may not exist (attacker probe)
but audit_log has NO FK on tenant_id — by design, so failures from
bogus claimed tenants still land on the journal.

Latency discipline
------------------
The 204 / 401 / 400 response is returned synchronously. Dedup claim is
a single INSERT round-trip, which is small enough to stay inside the
request path (CR-1 is 2 s). Audit writes + adapter dispatch run in
FastAPI BackgroundTasks so we never block the caller on anything that
isn't load-bearing for the response code or header.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Request, Response, status
from pydantic import ValidationError

from app.audit import (
    DEDUP_HIT,
    DEDUP_MISS,
    SIGNATURE_FAILED,
    WEBHOOK_RECEIVED_CALL_ANALYZED,
    WEBHOOK_RECEIVED_CALL_ENDED,
    WEBHOOK_RECEIVED_CALL_STARTED,
    WEBHOOK_RECEIVED_MALFORMED_JSON,
    WEBHOOK_RECEIVED_UNKNOWN,
    write_audit,
)
from app.call_pipeline import process_call_analyzed
from app.config import get_settings
from app.dedup import claim_event
from app.logging import get_logger
from app.models.retell import RetellWebhookPayload
from app.signature import verify_retell_signature

if TYPE_CHECKING:
    from asyncpg.pool import Pool

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Background-task helpers
# ---------------------------------------------------------------------------
async def _record_audit(
    pool: Pool,
    tenant_id: UUID,
    event_type: str,
    call_id: str | None,
    payload: dict[str, Any],
    source_ip: str | None,
) -> None:
    """Generic audit-row writer used by every branch after C3.

    Acquires a connection from the pool, opens a tx, pins
    `app.tenant_id` to the claimed path-param (so audit_log RLS admits
    the INSERT), writes the row, commits.

    Swallowed exceptions: an audit-write failure must NOT bubble — by
    the time this task runs the HTTP response has already gone out, so
    raising here just floods the structured log without any actionable
    way for the caller to react. We log.error and move on.
    """
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
                )
                await write_audit(
                    conn,
                    event_type=event_type,
                    tenant_id=tenant_id,
                    call_id=call_id,
                    actor="fastapi:webhook_receiver",
                    payload=payload,
                    trace_id="0",  # real trace_id wires in C8
                    source_ip=source_ip,
                )
    except Exception as exc:  # noqa: BLE001 — background path, never re-raise
        log.error(
            "webhook.audit_write_failed",
            event_type=event_type,
            tenant_id=str(tenant_id),
            error=str(exc),
        )


async def _record_signature_failure(
    pool: Pool,
    tenant_id: UUID,
    reason: str,
    header_present: bool,
    body_length: int,
    source_ip: str | None,
) -> None:
    """Background-task helper — writes one `signature.failed` audit row.

    Thin wrapper over `_record_audit` with the signature-specific payload
    shape frozen in place (C3 tests assert on `reason` / `header_present`
    / `body_length`).
    """
    await _record_audit(
        pool,
        tenant_id=tenant_id,
        event_type=SIGNATURE_FAILED,
        call_id=None,
        payload={
            "reason": reason,
            "header_present": header_present,
            "body_length": body_length,
        },
        source_ip=source_ip,
    )


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------
@router.post("/retell/{tenant_id}")
async def retell_webhook(
    tenant_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """Receive a Retell webhook; verify, dedup, route.

    The {tenant_id} path param is the CLAIMED tenant. It is authenticated
    via the HMAC signature (the workspace API key used to sign the body
    is tenant-scoped in production — v1 uses one workspace key for all
    tenants, which is fine because the tenant_id is still observable in
    the audit trail for forensics).

    Critical: read the raw body BEFORE any parse/reserialize. Re-encoding
    a dict reorders keys and changes whitespace, invalidating the HMAC.
    """
    started_at = time.perf_counter()
    settings = get_settings()
    pool: Pool = request.app.state.db_pool

    # MUST come before any json-decode step.
    raw_body = await request.body()
    header = request.headers.get("x-retell-signature")
    header_present = header is not None and header != ""
    source_ip = request.client.host if request.client is not None else None

    # -- 1. HMAC signature verification (C3) --------------------------------
    sig_result = verify_retell_signature(
        raw_body=raw_body,
        signature_header=header,
        api_key=settings.retell_api_key,
        skew_seconds=settings.webhook_timestamp_skew_seconds,
    )
    if not sig_result.is_valid:
        background_tasks.add_task(
            _record_signature_failure,
            pool,
            tenant_id,
            sig_result.reason or "unknown",
            header_present,
            len(raw_body),
            source_ip,
        )
        log.info(
            "webhook.signature.rejected",
            tenant_id=str(tenant_id),
            reason=sig_result.reason,
            header_present=header_present,
            source_ip=source_ip,
        )
        return Response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={
                "X-REIG-Trace-Id": "0",
                "X-REIG-Dedup-Status": "miss",
            },
        )

    # -- 2. JSON decode via Pydantic (C4) ----------------------------------
    try:
        payload = RetellWebhookPayload.model_validate_json(raw_body)
    except ValidationError as exc:
        # Malformed body: signature passed (so bytes came from a trusted
        # signer) but the shape isn't parseable. 400 + audit the event;
        # do NOT 500 / crash the pod.
        background_tasks.add_task(
            _record_audit,
            pool,
            tenant_id,
            WEBHOOK_RECEIVED_MALFORMED_JSON,
            None,
            {
                "reason": "validation_error",
                "errors": [
                    {"loc": list(e.get("loc", [])), "msg": e.get("msg")}
                    for e in exc.errors()
                ],
                "body_length": len(raw_body),
            },
            source_ip,
        )
        log.info(
            "webhook.malformed_json",
            tenant_id=str(tenant_id),
            body_length=len(raw_body),
        )
        return Response(
            status_code=status.HTTP_400_BAD_REQUEST,
            headers={
                "X-REIG-Trace-Id": "0",
                "X-REIG-Dedup-Status": "miss",
            },
        )

    event_type = payload.event
    call_id = payload.call.call_id

    # -- 3. Idempotent claim (C4) ------------------------------------------
    # Build the raw-payload dict ONCE so we can thread it into both the
    # processed_events row and any downstream audit payload without
    # re-parsing.
    raw_payload_dict = payload.model_dump(mode="json")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            dedup = await claim_event(
                conn,
                tenant_id=tenant_id,
                call_id=call_id,
                event_type=event_type,
                raw_payload=raw_payload_dict,
            )

    # -- 4. Audit + dispatch (C4) ------------------------------------------
    if dedup.status == "hit":
        background_tasks.add_task(
            _record_audit,
            pool,
            tenant_id,
            DEDUP_HIT,
            call_id,
            {"event": event_type},
            source_ip,
        )
        return _ok_response(started_at, settings, "hit")

    # status == "miss" — record the miss, then route by event type.
    background_tasks.add_task(
        _record_audit,
        pool,
        tenant_id,
        DEDUP_MISS,
        call_id,
        {"event": event_type},
        source_ip,
    )

    if event_type == "call_started":
        background_tasks.add_task(
            _record_audit,
            pool,
            tenant_id,
            WEBHOOK_RECEIVED_CALL_STARTED,
            call_id,
            {},
            source_ip,
        )
    elif event_type == "call_ended":
        background_tasks.add_task(
            _record_audit,
            pool,
            tenant_id,
            WEBHOOK_RECEIVED_CALL_ENDED,
            call_id,
            {},
            source_ip,
        )
    elif event_type == "call_analyzed":
        background_tasks.add_task(
            _record_audit,
            pool,
            tenant_id,
            WEBHOOK_RECEIVED_CALL_ANALYZED,
            call_id,
            {},
            source_ip,
        )
        # The ONLY event that fires the downstream pipeline (CR-4).
        background_tasks.add_task(
            process_call_analyzed,
            tenant_id,
            payload.call,
        )
    else:
        background_tasks.add_task(
            _record_audit,
            pool,
            tenant_id,
            WEBHOOK_RECEIVED_UNKNOWN,
            call_id,
            {"event": event_type},
            source_ip,
        )

    return _ok_response(started_at, settings, "miss")


def _ok_response(started_at: float, settings: Any, dedup_status: str) -> Response:
    """Build the 204 response + latency log.

    Split out so the `hit` and `miss` branches share the same end-of-
    request shape without duplicating the elapsed-ms computation or the
    header dict.
    """
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    if elapsed_ms > settings.webhook_response_sla_seconds * 1000:
        log.warning(
            "webhook.latency_exceeded",
            elapsed_ms=elapsed_ms,
            sla_ms=settings.webhook_response_sla_seconds * 1000,
            dedup_status=dedup_status,
        )
    else:
        log.info(
            "webhook.accepted",
            elapsed_ms=elapsed_ms,
            dedup_status=dedup_status,
        )

    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={
            "X-REIG-Trace-Id": "0",
            "X-REIG-Dedup-Status": dedup_status,
        },
    )
