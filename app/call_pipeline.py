"""Call-analyzed processing pipeline.

C4 shipped this as a logging no-op. C5 wires the adapter resolver
through the live path (resolve → map_fields) but intentionally STOPS
before `upsert_record` because Salesforce's implementation arrives in
C7. The gap is marked with a `TODO(C7)` comment below.

Contract notes
--------------
* Must be SAFE TO CALL N TIMES with the same `(tenant_id, call_id)`.
  Today that's trivially true because we log and exit before any
  downstream side effect. Once C7 adds `upsert_record` it remains
  true because SFDC upsert is idempotent on `External_Call_Id__c`
  and our `crm_writes` table keys on the same tuple.

* Must not raise on the happy path. Errors inside the adapter (once
  C7 lands) will be captured into `crm_writes.status='failed'` + an
  audit row, not bubbled to the webhook response (which already
  returned 204). In C5, any unexpected exception is logged and
  swallowed — a BackgroundTask that raises is invisible to the
  request path but spams structlog with tracebacks.

* Runs inside a FastAPI BackgroundTask — no `Request` object, so all
  state threads through the args. The pool is passed by the route
  handler from `request.app.state.db_pool`.

C5 live wiring
--------------
1. Acquire a conn from the passed-in pool.
2. Open a tx + `SET LOCAL app.tenant_id` so any adapter call that
   touches a tenant-scoped table respects RLS.
3. `resolve_adapter` → returns the tenant's configured CRMAdapter.
4. `adapter.map_fields(payload)` — pure, already implemented for
   Salesforce. Log the resulting LeadUpsertPayload fields.
5. Log an `adapter.upsert.deferred` structlog event — C7's test
   flips this assertion to "actually fire", which is exactly the
   shape C7 needs.

Why not call `authenticate` in C5: the stub is a no-op, so there's
nothing to prove by calling it. Tests in C5 exercise `authenticate`
directly against the concrete class.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from app.adapter_resolver import resolve_adapter
from app.config import get_settings
from app.logging import get_logger
from app.models.retell import RetellCall

if TYPE_CHECKING:
    from asyncpg.pool import Pool

log = get_logger(__name__)


async def process_call_analyzed(
    tenant_id: UUID,
    call_payload: RetellCall,
    pool: Pool,
) -> None:
    """Handle a deduped `call_analyzed` event.

    C5 scope: resolve the tenant's adapter, log the mapped payload,
    record a deferred-upsert event. Does NOT call `upsert_record`
    (Salesforce's impl arrives in C7 and would raise
    NotImplementedError inside the BackgroundTask).

    Args:
        tenant_id:    The tenant who owns this call.
        call_payload: The validated `call` sub-object from the webhook.
                      `call_id` is load-bearing; everything else is
                      optional and may be None on early-arriving events.
        pool:         asyncpg pool attached to `app.state.db_pool` at
                      startup. Passed in so this function is unit-
                      testable with a fake pool rather than a
                      `from app.main import app` coupling.
    """
    log.info(
        "call_pipeline.process_call_analyzed.invoked",
        tenant_id=str(tenant_id),
        call_id=call_payload.call_id,
        has_transcript=call_payload.transcript is not None,
        to_number_present=call_payload.to_number is not None,
    )

    settings = get_settings()

    # Full payload dict. `RetellCall` has `extra="allow"`, so unmodelled
    # fields are preserved by `.model_dump(mode="json")` — the adapter
    # sees exactly what Retell sent.
    call_dict: dict[str, object] = {
        "call": call_payload.model_dump(mode="json"),
    }

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
                )
                adapter = await resolve_adapter(tenant_id, conn, settings)
                # `map_fields` is pure — no network, no DB. Safe in the tx.
                payload = await adapter.map_fields(call_dict)

        log.info(
            "call_pipeline.adapter.mapped",
            tenant_id=str(tenant_id),
            call_id=call_payload.call_id,
            external_call_id=payload.external_call_id,
            has_phone=payload.phone is not None,
            has_description=payload.description is not None,
            company=payload.company,
            lead_source=payload.lead_source,
        )

        # C5 stops here. C7 replaces this log with the actual upsert call:
        #
        #   await adapter.authenticate()
        #   result = await adapter.upsert_record(payload)
        #   # write crm_writes row, audit success/fail
        #
        # TODO(C7): swap the deferred log for the live upsert + crm_writes.
        log.info(
            "adapter.upsert.deferred",
            tenant_id=str(tenant_id),
            call_id=call_payload.call_id,
            external_call_id=payload.external_call_id,
            reason="C5: upsert_record lands in C7",
        )
    except Exception as exc:  # noqa: BLE001 — background path, never re-raise
        log.error(
            "call_pipeline.process_call_analyzed.failed",
            tenant_id=str(tenant_id),
            call_id=call_payload.call_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )

    return None
