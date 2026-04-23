"""Call-analyzed processing pipeline.

C4 shipped a logging no-op. C5 wired the adapter resolver + map_fields.
C6 added Presidio redaction. C7 now flips the `adapter.upsert.deferred`
log into an actual `upsert_record` call that writes a `crm_writes` row
on both success and failure, plus audit events for forensics.

Contract notes
--------------
* Safe to call N times with the same `(tenant_id, call_id)`. The SFDC
  upsert is idempotent on `External_Call_Id__c`, and `crm_writes` rows
  are append-only (one row per attempt).

* Never raises on the happy OR sad path — the webhook already returned
  204 before this BackgroundTask fires. A Salesforce outage must land
  a `failed` row + log lines, not crash the worker.

* Runs inside a FastAPI BackgroundTask. The httpx client inside the
  adapter is instance-scoped, so nothing leaks across tasks.
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from tenacity import RetryError

from adapters.errors import AdapterError, PermanentError, TransientError
from app.adapter_resolver import resolve_adapter
from app.audit import (
    ADAPTER_UPSERT_EXHAUSTED,
    ADAPTER_UPSERT_PERMANENT,
    ADAPTER_UPSERT_SUCCESS,
    write_audit,
)
from app.config import get_settings
from app.db import set_encryption_key
from app.logging import get_logger
from app.models.retell import RetellCall
from app.pii import redact

if TYPE_CHECKING:
    import asyncpg
    from asyncpg.pool import Pool

log = get_logger(__name__)


async def _write_crm_success(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    call_id: str,
    adapter_name: str,
    record_id: str,
    attempts: int,
    duration_ms: float,
) -> None:
    """Append one `crm_writes` row with status='success'."""
    await conn.execute(
        "INSERT INTO crm_writes "
        "(tenant_id, call_id, adapter, status, sfdc_lead_id, attempts, "
        " error_context, created_at, updated_at) "
        "VALUES ($1, $2, $3, 'success', $4, $5, $6::jsonb, now(), now())",
        tenant_id,
        call_id,
        adapter_name,
        record_id,
        attempts,
        json.dumps({"duration_ms": round(duration_ms, 2)}),
    )


async def _write_crm_failure(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    call_id: str,
    adapter_name: str,
    attempts: int,
    error_context: dict[str, Any],
) -> None:
    """Append one `crm_writes` row with status='failed'."""
    await conn.execute(
        "INSERT INTO crm_writes "
        "(tenant_id, call_id, adapter, status, sfdc_lead_id, attempts, "
        " error_context, created_at, updated_at) "
        "VALUES ($1, $2, $3, 'failed', NULL, $4, $5::jsonb, now(), now())",
        tenant_id,
        call_id,
        adapter_name,
        attempts,
        json.dumps(error_context, default=str),
    )


def _unwrap_retry_error(exc: RetryError) -> tuple[str, int]:
    """Extract the final exception + attempt count from a tenacity RetryError."""
    stats = exc.last_attempt
    try:
        attempts = stats.attempt_number
    except AttributeError:
        attempts = 1
    try:
        last_exc = stats.exception()
    except Exception:  # noqa: BLE001 — best-effort diagnostics
        last_exc = None
    detail = str(last_exc) if last_exc is not None else "unknown"
    return detail, int(attempts or 1)


async def process_call_analyzed(
    tenant_id: UUID,
    call_payload: RetellCall,
    pool: Pool,
) -> None:
    """Handle a deduped `call_analyzed` event end-to-end (C6 + C7).

    1. Redact the transcript via Presidio.
    2. Persist the redacted copy + summary metadata into `calls`.
    3. Resolve the adapter, authenticate, upsert_record.
    4. Write `crm_writes` + audit event based on outcome.
    """
    log.info(
        "call_pipeline.process_call_analyzed.invoked",
        tenant_id=str(tenant_id),
        call_id=call_payload.call_id,
        has_transcript=call_payload.transcript is not None,
    )

    settings = get_settings()
    adapter_name = settings.active_adapter

    # ---- PII redaction (C6 / CR-10) -------------------------------------
    raw_transcript = call_payload.transcript or ""
    redaction = redact(raw_transcript)
    call_dict_raw = call_payload.model_dump(mode="json")
    call_dict_raw["transcript"] = redaction.text
    call_dict: dict[str, object] = {"call": call_dict_raw}

    # ---- Persist call metadata + resolve adapter ------------------------
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
                )
                await set_encryption_key(conn)

                metadata = {
                    "redacted_transcript": redaction.text,
                    "pii_entities_removed": redaction.entities_removed,
                    "pii_entity_counts": redaction.entity_counts,
                    "from_number": call_payload.from_number,
                    "to_number": call_payload.to_number,
                    "agent_id": call_payload.agent_id,
                }
                await conn.execute(
                    "INSERT INTO calls (tenant_id, call_id, metadata) "
                    "VALUES ($1, $2, $3::jsonb) "
                    "ON CONFLICT (tenant_id, call_id) DO UPDATE "
                    "SET metadata = EXCLUDED.metadata",
                    tenant_id,
                    call_payload.call_id,
                    json.dumps(metadata, default=str),
                )

                # `resolve_adapter` reads tenants.active_adapter; we pass
                # the pool explicitly because the adapter needs its own
                # connections for authenticate() (which opens its own tx).
                adapter = await resolve_adapter(tenant_id, conn, settings)
                # Late-bind the pool so authenticate()/upsert_record() can
                # acquire their own conns (can't reuse `conn` — it's about
                # to commit below).
                adapter.db_pool = pool  # type: ignore[attr-defined]

                payload = await adapter.map_fields(call_dict)
    except Exception as exc:  # noqa: BLE001 — background path, never re-raise
        log.error(
            "call_pipeline.setup_failed",
            tenant_id=str(tenant_id),
            call_id=call_payload.call_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None

    # ---- Adapter authenticate + upsert_record (C7 / CR-8 + CR-9) --------
    attempts = 1
    t_start = time.perf_counter()
    try:
        await adapter.authenticate()
        result = await adapter.upsert_record(payload)
    except PermanentError as exc:
        duration_ms = (time.perf_counter() - t_start) * 1000
        err_context = {
            "error_type": "PermanentError",
            "message": str(exc)[:1000],
            "duration_ms": round(duration_ms, 2),
        }
        log.warning(
            "call_pipeline.adapter.permanent_failure",
            tenant_id=str(tenant_id),
            call_id=call_payload.call_id,
            error=str(exc)[:300],
        )
        await _record_failure(
            pool,
            tenant_id,
            call_payload.call_id,
            adapter_name,
            attempts,
            err_context,
            event_type=ADAPTER_UPSERT_PERMANENT,
        )
        return None
    except RetryError as exc:
        duration_ms = (time.perf_counter() - t_start) * 1000
        detail, attempts = _unwrap_retry_error(exc)
        err_context = {
            "error_type": "RetryError",
            "attempts": attempts,
            "message": detail[:1000],
            "duration_ms": round(duration_ms, 2),
        }
        log.warning(
            "call_pipeline.adapter.retry_exhausted",
            tenant_id=str(tenant_id),
            call_id=call_payload.call_id,
            attempts=attempts,
        )
        await _record_failure(
            pool,
            tenant_id,
            call_payload.call_id,
            adapter_name,
            attempts,
            err_context,
            event_type=ADAPTER_UPSERT_EXHAUSTED,
        )
        return None
    except (AdapterError, Exception) as exc:  # noqa: BLE001 — catch-all
        duration_ms = (time.perf_counter() - t_start) * 1000
        err_context = {
            "error_type": type(exc).__name__,
            "message": str(exc)[:1000],
            "duration_ms": round(duration_ms, 2),
        }
        log.error(
            "call_pipeline.adapter.unexpected_failure",
            tenant_id=str(tenant_id),
            call_id=call_payload.call_id,
            error=str(exc)[:300],
            error_type=type(exc).__name__,
        )
        await _record_failure(
            pool,
            tenant_id,
            call_payload.call_id,
            adapter_name,
            attempts,
            err_context,
            event_type=ADAPTER_UPSERT_EXHAUSTED,
        )
        return None

    duration_ms = (time.perf_counter() - t_start) * 1000
    await _record_success(
        pool,
        tenant_id,
        call_payload.call_id,
        adapter_name,
        result.record_id,
        attempts,
        duration_ms,
        status_word=result.status,
    )
    return None


# Avoid unused-import warnings when the exception type is referenced only
# in `except` handlers above.
_ = TransientError


async def _record_success(
    pool: Pool,
    tenant_id: UUID,
    call_id: str,
    adapter_name: str,
    record_id: str,
    attempts: int,
    duration_ms: float,
    status_word: str,
) -> None:
    """Open a new tx, write `crm_writes` + `adapter.upsert.success` audit row."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            await _write_crm_success(
                conn,
                tenant_id,
                call_id,
                adapter_name,
                record_id,
                attempts,
                duration_ms,
            )
            await write_audit(
                conn,
                event_type=ADAPTER_UPSERT_SUCCESS,
                tenant_id=tenant_id,
                call_id=call_id,
                actor="adapter:salesforce",
                payload={
                    "record_id": record_id,
                    "status": status_word,
                    "attempts": attempts,
                    "duration_ms": round(duration_ms, 2),
                },
                trace_id="0",
                source_ip=None,
            )
    log.info(
        "adapter.upsert.ok",
        tenant_id=str(tenant_id),
        call_id=call_id,
        record_id=record_id,
        attempts=attempts,
        duration_ms=round(duration_ms, 2),
        status=status_word,
    )


async def _record_failure(
    pool: Pool,
    tenant_id: UUID,
    call_id: str,
    adapter_name: str,
    attempts: int,
    error_context: dict[str, Any],
    event_type: str,
) -> None:
    """Open a new tx, write `crm_writes.status='failed'` + audit."""
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
                )
                await _write_crm_failure(
                    conn, tenant_id, call_id, adapter_name, attempts, error_context
                )
                await write_audit(
                    conn,
                    event_type=event_type,
                    tenant_id=tenant_id,
                    call_id=call_id,
                    actor="adapter:salesforce",
                    payload=error_context,
                    trace_id="0",
                    source_ip=None,
                )
    except Exception as exc:  # noqa: BLE001 — background path, never re-raise
        log.error(
            "call_pipeline.record_failure.write_failed",
            tenant_id=str(tenant_id),
            call_id=call_id,
            error=str(exc),
        )
