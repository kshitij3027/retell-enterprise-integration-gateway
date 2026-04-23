"""Call-analyzed processing pipeline — stub for C4.

In C4 we wire routing: when a `call_analyzed` webhook arrives and wins
the dedup claim, we dispatch a BackgroundTask that calls
`process_call_analyzed`. For C4 this function is a no-op after logging:
the real adapter work (resolve the tenant's `active_adapter`, map fields,
upsert the SFDC Lead, mark crm_writes) lands in C5..C7.

Contract notes
--------------
* Must be SAFE TO CALL N TIMES with the same `(tenant_id, call_id)`.
  Today that's trivially true because we don't do anything; once C7 adds
  the adapter upsert it continues to be true because SFDC upsert is
  idempotent on `External_Call_Id__c` and our crm_writes table keys on
  the same tuple.

* Must not raise on the happy path. Errors inside the adapter will be
  captured into `crm_writes.status='failed'` + audit rows in C7, not
  bubbled to the webhook response (which already returned 204).

* Runs inside a FastAPI BackgroundTask — no `Request` object available,
  so all state threads through the args. The pool comes from
  `app.state.db_pool` at schedule time (see routes/webhooks.py).
"""
from __future__ import annotations

from uuid import UUID

from app.logging import get_logger
from app.models.retell import RetellCall

log = get_logger(__name__)


async def process_call_analyzed(
    tenant_id: UUID,
    call_payload: RetellCall,
) -> None:
    """Handle a deduped `call_analyzed` event.

    C4 scope: log the intent and exit. C5 will resolve the tenant's
    adapter via `tenants.active_adapter`; C6 will redact PII out of
    `call_payload.transcript`; C7 will call `adapter.upsert_record(...)`
    and write a `crm_writes` row.

    Args:
        tenant_id:    The tenant who owns this call.
        call_payload: The validated `call` sub-object from the webhook.
                      `call_id` is load-bearing; everything else is
                      optional and may be None on early-arriving events.
    """
    log.info(
        "call_pipeline.process_call_analyzed.invoked",
        tenant_id=str(tenant_id),
        call_id=call_payload.call_id,
        has_transcript=call_payload.transcript is not None,
        to_number_present=call_payload.to_number is not None,
    )
    # C5+ wiring lands here. The function is intentionally a no-op for C4
    # so dedup tests can assert "called exactly once" without a live
    # adapter in the loop.
    return None
