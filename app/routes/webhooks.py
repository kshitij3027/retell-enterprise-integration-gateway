"""Retell webhook receiver — signature-verification path (C3).

CR-1: respond within 2 s. CR-2: HMAC verify, 5-min skew, 401 on any
failure, audit row written on fail.

Scope of THIS file in C3:
  * `POST /webhooks/retell/{tenant_id}` — verify HMAC, 204 on pass, 401
    on fail. Dedup + event-type routing land in C4.

Why this route is exempt from TenantResolutionMiddleware:
  Retell signs the body with the tenant's workspace API key (HMAC). The
  signature IS the authentication — there's no `X-API-Key` header on
  these requests. The middleware's exemption list already covers
  `/webhooks/retell/` (see app/middleware/tenant.py `_EXEMPT_PREFIXES`).

Why the audit write bypasses `get_db`:
  `get_db` requires `request.state.tenant_id` — which the middleware
  skipped setting because this path is exempt. We acquire directly from
  the pool, open our own short-lived transaction, and set
  `app.tenant_id = <claimed path-param>` so the audit_log INSERT passes
  its RLS WITH CHECK. The claimed tenant may not exist (attacker probe)
  but audit_log has NO FK on tenant_id — by design, so failures from
  bogus claimed tenants still land on the journal.

Latency discipline:
  The 401/204 response is returned synchronously. The audit write runs
  in a FastAPI BackgroundTask so we never block the caller on a DB
  round-trip. Clock is measured around the synchronous portion only.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Request, Response, status

from app.audit import write_audit
from app.config import get_settings
from app.logging import get_logger
from app.signature import verify_retell_signature

if TYPE_CHECKING:
    from asyncpg.pool import Pool

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _record_signature_failure(
    pool: Pool,
    tenant_id: UUID,
    reason: str,
    header_present: bool,
    body_length: int,
    source_ip: str | None,
) -> None:
    """Background-task helper — writes one `signature.failed` audit row.

    Acquires a short-lived connection from the app pool, opens a tx,
    pins `app.tenant_id` to the claimed path-param so the audit_log RLS
    policy admits the INSERT, writes the row, commits.

    Exceptions are caught and logged — an audit-write failure must NOT
    crash the request path (the HTTP 401 has already gone out by the
    time this task runs).
    """
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
                )
                await write_audit(
                    conn,
                    event_type="signature.failed",
                    tenant_id=tenant_id,
                    call_id=None,
                    actor="fastapi:webhook_receiver",
                    payload={
                        "reason": reason,
                        "header_present": header_present,
                        "body_length": body_length,
                    },
                    trace_id="0",  # real trace_id wires in C8
                    source_ip=source_ip,
                )
    except Exception as exc:  # noqa: BLE001 — background path, never re-raise
        log.error(
            "webhook.audit_write_failed",
            reason=reason,
            tenant_id=str(tenant_id),
            error=str(exc),
        )


@router.post("/retell/{tenant_id}")
async def retell_webhook(
    tenant_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """Receive a Retell webhook; verify HMAC; 204 on pass, 401 on fail.

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

    # MUST come before any json-decode step.
    raw_body = await request.body()
    header = request.headers.get("x-retell-signature")
    header_present = header is not None and header != ""
    source_ip = request.client.host if request.client is not None else None

    result = verify_retell_signature(
        raw_body=raw_body,
        signature_header=header,
        api_key=settings.retell_api_key,
        skew_seconds=settings.webhook_timestamp_skew_seconds,
    )

    if not result.is_valid:
        # Schedule audit write AFTER the 401 is returned so we don't block the
        # response on a DB round-trip. FastAPI runs BackgroundTasks after
        # the response is flushed.
        pool: Pool = request.app.state.db_pool
        background_tasks.add_task(
            _record_signature_failure,
            pool,
            tenant_id,
            result.reason or "unknown",
            header_present,
            len(raw_body),
            source_ip,
        )
        log.info(
            "webhook.signature.rejected",
            tenant_id=str(tenant_id),
            reason=result.reason,
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

    # Pass path — dedup + routing land in C4. For now: 204.
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    if elapsed_ms > settings.webhook_response_sla_seconds * 1000:
        log.warning(
            "webhook.latency_exceeded",
            tenant_id=str(tenant_id),
            elapsed_ms=elapsed_ms,
            sla_ms=settings.webhook_response_sla_seconds * 1000,
        )
    else:
        log.info(
            "webhook.accepted",
            tenant_id=str(tenant_id),
            elapsed_ms=elapsed_ms,
        )

    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={
            "X-REIG-Trace-Id": "0",
            "X-REIG-Dedup-Status": "miss",
        },
    )
