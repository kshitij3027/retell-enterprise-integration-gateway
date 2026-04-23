"""Call-analyzed processing pipeline.

C4 shipped this as a logging no-op. C5 wired the adapter resolver
through the live path (resolve → map_fields). C6 now redacts the
transcript BEFORE it flows into the adapter and persists a row in
`calls.metadata` so a reviewer can see the redaction happened (the
raw transcript never leaves `redact()` scope).

C7 will flip the `deferred` log into an actual `upsert_record` call +
`crm_writes` row.

Contract notes
--------------
* Must be SAFE TO CALL N TIMES with the same `(tenant_id, call_id)`.
  The `calls` upsert uses `ON CONFLICT (tenant_id, call_id) DO UPDATE`
  so replays overwrite the metadata row rather than duplicating it.

* Must not raise on the happy path. Errors inside the adapter (once
  C7 lands) will be captured into `crm_writes.status='failed'` + an
  audit row, not bubbled to the webhook response (which already
  returned 204).

* Runs inside a FastAPI BackgroundTask — no `Request` object, so all
  state threads through the args.

C6 live wiring
--------------
1. Acquire a conn from the passed-in pool.
2. Open a tx + `SET LOCAL app.tenant_id` so writes on tenant-scoped
   tables respect RLS.
3. Redact `call_payload.transcript` via Presidio. Passing None-or-
   empty just short-circuits to the empty result.
4. Thread the redacted text into the dict we pass to `adapter.map_fields`
   — `map_fields` is PII-unaware by design; C6 wraps it here so the
   adapter file stays pure.
5. UPSERT one row into `calls` with the redacted transcript in
   `metadata->>'redacted_transcript'` and an entities-removed count.
6. Log `adapter.upsert.deferred` (C7 replaces with the real upsert).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

from app.adapter_resolver import resolve_adapter
from app.config import get_settings
from app.logging import get_logger
from app.models.retell import RetellCall
from app.pii import redact

if TYPE_CHECKING:
    from asyncpg.pool import Pool

log = get_logger(__name__)


async def process_call_analyzed(
    tenant_id: UUID,
    call_payload: RetellCall,
    pool: Pool,
) -> None:
    """Handle a deduped `call_analyzed` event with PII redaction.

    C6 scope: redact the transcript BEFORE it hits the adapter, persist
    the redacted copy in `calls.metadata` for replay/audit, resolve the
    adapter, call `map_fields` with the redacted payload. Does NOT call
    `upsert_record` (C7 lands that).

    Args:
        tenant_id:    The tenant who owns this call.
        call_payload: The validated `call` sub-object from the webhook.
                      `call_id` is load-bearing; everything else optional.
        pool:         asyncpg pool attached to `app.state.db_pool`.
    """
    log.info(
        "call_pipeline.process_call_analyzed.invoked",
        tenant_id=str(tenant_id),
        call_id=call_payload.call_id,
        has_transcript=call_payload.transcript is not None,
        to_number_present=call_payload.to_number is not None,
    )

    settings = get_settings()

    # ---- PII redaction (C6 / CR-10) -------------------------------------
    # Redact BEFORE we build the dict we pass to map_fields. `map_fields`
    # is pure and PII-unaware; Presidio lives here at the pipeline seam.
    # Empty / None transcripts short-circuit inside `redact()` to the
    # empty-result case.
    raw_transcript = call_payload.transcript or ""
    redaction = redact(raw_transcript)

    # Build the dict the adapter sees — with the redacted transcript
    # substituted in. We must NOT leak the original past this point.
    call_dict_raw = call_payload.model_dump(mode="json")
    call_dict_raw["transcript"] = redaction.text
    call_dict: dict[str, object] = {"call": call_dict_raw}

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
                )

                # Persist the redacted transcript + summary metadata. Upsert
                # on (tenant_id, call_id) so replays overwrite cleanly.
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

                adapter = await resolve_adapter(tenant_id, conn, settings)
                # `map_fields` is pure — no network, no DB. Safe in tx.
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
            pii_entities_removed=redaction.entities_removed,
        )

        # C6 stops here. C7 replaces with: authenticate + upsert_record +
        # crm_writes row + audit success/fail.
        # TODO(C7): swap the deferred log for the live upsert + crm_writes.
        log.info(
            "adapter.upsert.deferred",
            tenant_id=str(tenant_id),
            call_id=call_payload.call_id,
            external_call_id=payload.external_call_id,
            reason="C6: upsert_record lands in C7",
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
