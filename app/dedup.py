"""Idempotency / dedup claim (CR-3).

Retell retries webhooks aggressively on non-2xx — up to several times per
minute — and network flakes mean the same `(tenant_id, call_id, event_type)`
tuple arrives 2..N times legitimately. The contract: exactly one downstream
side effect, every time.

Mechanism
---------
`processed_events` has `UNIQUE(tenant_id, call_id, event_type)`. `claim_event`
does an INSERT ... ON CONFLICT DO NOTHING RETURNING id. Postgres takes a row-
level lock on the conflicting row, so concurrent INSERTs of the same key
linearize: exactly one session's INSERT returns a row (the winner, "miss"),
every other session's INSERT returns zero rows (the losers, "hit").

Why NOT advisory locks
----------------------
We considered `pg_try_advisory_xact_lock(hashtext(key))` + read-after-lock
— but that's two round-trips (lock + check) and introduces hash collisions
on the bigint key space. ON CONFLICT is a single statement, enforced by the
unique constraint the schema already has, and the index is the lock.

Concurrency note
----------------
The row-level lock is held for the duration of the ENCLOSING transaction.
The caller's transaction should be SHORT — open it, claim, commit. We do
NOT do the adapter upsert inside the same transaction (that would hold the
lock across an HTTP round-trip to Salesforce). C7's `process_call_analyzed`
runs in its own BackgroundTask after the claim commits.

RLS preconditions
-----------------
The caller MUST have already set `app.tenant_id` on `conn` (either via
`get_db` or a bare `SET LOCAL`). This function trusts that — it does not
manage session variables itself, so it can compose inside a larger tx.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from app.logging import get_logger

if TYPE_CHECKING:
    import asyncpg

log = get_logger(__name__)


@dataclass(frozen=True)
class DedupResult:
    """Outcome of a `claim_event` call.

    Attributes:
        status: "miss" when this caller won the insert (first to claim the
                tuple); "hit" when another tx already claimed it.
        row_id: The `processed_events.id` of the winning row on "miss";
                None on "hit" (we don't bother reading the existing row's
                id — the caller only needs to know "someone else got it").
    """

    status: Literal["miss", "hit"]
    row_id: UUID | None


async def claim_event(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    call_id: str,
    event_type: str,
    raw_payload: dict[str, Any],
) -> DedupResult:
    """Atomically claim `(tenant_id, call_id, event_type)` for processing.

    The INSERT is `ON CONFLICT DO NOTHING RETURNING id` — a single
    statement that serializes concurrent claimants through the unique
    index. The winner gets the row id back in RETURNING; losers get zero
    rows.

    Args:
        conn:        asyncpg connection with `app.tenant_id` already pinned.
                     Must be inside a transaction (processed_events has RLS
                     and the INSERT's WITH CHECK requires the tenant var).
        tenant_id:   The tenant claiming the event. Must match the session var.
        call_id:     Retell `call.call_id` from the payload.
        event_type:  `"call_started"` | `"call_ended"` | `"call_analyzed"` |
                     any other Retell event name (we dedup all of them — the
                     event-type routing in webhooks.py is a separate concern).
        raw_payload: The full webhook body as a dict. Stored in
                     `raw_payload jsonb` so replayed webhooks can be
                     forensically compared to the one we accepted.

    Returns:
        DedupResult(status="miss", row_id=<uuid>) on first claim.
        DedupResult(status="hit", row_id=None)    on subsequent replays.
    """
    row = await conn.fetchrow(
        "INSERT INTO processed_events "
        "(id, tenant_id, call_id, event_type, raw_payload, created_at) "
        "VALUES (gen_random_uuid(), $1, $2, $3, $4::jsonb, NOW()) "
        "ON CONFLICT (tenant_id, call_id, event_type) DO NOTHING "
        "RETURNING id",
        tenant_id,
        call_id,
        event_type,
        json.dumps(raw_payload, default=str),
    )

    if row is None:
        log.debug(
            "dedup.hit",
            tenant_id=str(tenant_id),
            call_id=call_id,
            event_type=event_type,
        )
        return DedupResult(status="hit", row_id=None)

    row_id: UUID = row["id"]
    log.debug(
        "dedup.miss",
        tenant_id=str(tenant_id),
        call_id=call_id,
        event_type=event_type,
        row_id=str(row_id),
    )
    return DedupResult(status="miss", row_id=row_id)
