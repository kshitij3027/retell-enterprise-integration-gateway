"""Append-only audit log writer (CR-14).

audit_log is a tamper-evident journal of every security-relevant event —
signature failures, dedup hits, adapter upserts, OAuth callbacks, etc.
The full event-type catalog lands in C4; this module is the write path
and only needs to cover `signature.failed` for C3.

Design notes:

  * audit_log has RLS enabled and UPDATE/DELETE revoked from `reig_app`,
    so even a compromised app role cannot rewrite history. INSERT is
    allowed as long as `app.tenant_id` matches the row's tenant_id (the
    policy's USING clause doubles as WITH CHECK when no explicit
    WITH CHECK is declared).

  * For `signature.failed` the claimed tenant comes from the URL path of
    an unauthenticated request — i.e. an attacker could claim any UUID.
    We log it anyway (logging the claim IS the point). The audit_log
    schema deliberately makes tenant_id nullable and FK-less (see
    migrations/0001_initial.sql line 134) exactly so we can record
    failures even for bogus claimed tenants.

  * `trace_id` is placeholder "0" until C8 wires OpenTelemetry — callers
    can pass any string; we treat it as opaque.

  * Payload is a dict → JSON-serialized by asyncpg's jsonb codec. We
    encode manually via `json.dumps` so mypy --strict sees a plain str
    flowing into the parameterized query.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.logging import get_logger

if TYPE_CHECKING:
    import asyncpg

log = get_logger(__name__)


async def write_audit(
    conn: asyncpg.Connection,
    event_type: str,
    tenant_id: UUID | None,
    call_id: str | None,
    actor: str,
    payload: dict[str, Any],
    trace_id: str,
    source_ip: str | None,
) -> None:
    """Insert one audit_log row using an already-RLS-scoped connection.

    The caller is responsible for opening the transaction and setting
    `app.tenant_id` to match `tenant_id` — this function does NOT manage
    its own tx. Keeping session-var management out of here means the
    caller can batch the audit write alongside other writes in a single
    transaction.

    Args:
        conn:       asyncpg connection with `app.tenant_id` already pinned
                    to `tenant_id` (so the RLS policy admits the INSERT).
        event_type: Dotted event name, e.g. "signature.failed".
        tenant_id:  The subject tenant (nullable — some events fire before
                    any tenant is known).
        call_id:    Retell call_id when applicable; None for pre-call events.
        actor:      Which subsystem produced the row, e.g.
                    "fastapi:webhook_receiver".
        payload:    Structured detail. JSON-serialized before storage.
        trace_id:   OTel trace id placeholder ("0" until C8).
        source_ip:  Client IP when known (Retell edge or ngrok); stored as
                    inet. None for internally-originated events.

    Returns:
        None. Errors bubble — callers in a BackgroundTask context will
        see them in logs, not on the request path.
    """
    await conn.execute(
        "INSERT INTO audit_log "
        "(tenant_id, event_type, call_id, actor, payload, trace_id, source_ip) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)",
        tenant_id,
        event_type,
        call_id,
        actor,
        json.dumps(payload, default=str),
        trace_id,
        source_ip,
    )
    log.debug(
        "audit.written",
        event_type=event_type,
        tenant_id=str(tenant_id) if tenant_id is not None else None,
        call_id=call_id,
        actor=actor,
    )
