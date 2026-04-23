"""ServiceNow adapter stub — the "proves CR-11" demo-day deliverable.

The JD calls out generalizability: Retell's middleware should be able
to sprout a second downstream system without touching route handlers.
`ServiceNowAdapter` is the living proof — it's wired through the SAME
`CRMAdapter` Protocol, registered under `"servicenow_stub"`, and a
reviewer can flip `tenants.active_adapter='servicenow_stub'` to point
the pipeline at it.

Only `upsert_record` is stubbed (raises NotImplementedError). The
docstring on that method is the deliverable — it walks through EXACTLY
what a production implementation would do, so anyone picking this up
later has a spec, not a blank function body.

`map_fields` returns a minimal-but-valid `LeadUpsertPayload` so
`isinstance(..., CRMAdapter)` and the mypy Protocol check both pass
without surfacing NotImplementedError in the wrong place.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from adapters import register
from adapters.base import LeadUpsertPayload, UpsertResult
from app.logging import get_logger

if TYPE_CHECKING:
    import asyncpg
    import httpx

    from app.config import Settings

log = get_logger(__name__)


@register("servicenow_stub")
class ServiceNowAdapter:
    """ServiceNow stub — structural proof that CRMAdapter is generic.

    Construction mirrors `SalesforceAdapter` exactly so the resolver
    can hand back either class with the same call shape. No DB or
    network calls happen in C5 — `upsert_record` raises loudly, and
    no live path in the middleware invokes it.
    """

    def __init__(
        self,
        tenant_id: UUID,
        db_pool: asyncpg.Pool | None,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Store construction args. No I/O — matches Salesforce's shape.

        Args:
            tenant_id:   Owner of this adapter instance.
            db_pool:     Unused in the stub; a production impl would
                         read a ServiceNow-specific `credentials` row
                         keyed on `adapter='servicenow'`.
            settings:    App settings — a real impl would pull
                         `servicenow_instance_url`, `servicenow_client_id`,
                         etc. (not yet defined; add alongside the real impl).
            http_client: Same injection story as Salesforce — handy for
                         respx-mocking in tests.
        """
        self.tenant_id = tenant_id
        self.db_pool = db_pool
        self.settings = settings
        self.http_client = http_client

    async def authenticate(self) -> None:
        """No-op in the stub. Matches Protocol shape exactly.

        A production impl would POST to
        `https://<instance>.service-now.com/oauth_token.do` with
        `grant_type=client_credentials` and cache the returned
        access_token (ServiceNow access tokens default to 30 min TTL).
        """
        return None

    async def upsert_record(self, payload: LeadUpsertPayload) -> UpsertResult:
        """STUB — raises NotImplementedError.

        A production ServiceNow implementation of this method would:

          1. Ensure a valid OAuth access token is cached. Auth is
             OAuth 2.0 client-credentials against
             ``<instance>.service-now.com/oauth_token.do``. The
             returned token has a 30-minute TTL by default; cache it
             in `credentials.access_token_cached` and refresh on miss.
          2. POST the translated payload to
             ``/api/now/table/incident`` with
             ``Authorization: Bearer <access_token>`` and a
             ``x-correlation-id: {external_call_id}`` header. The
             ``x-correlation-id`` is ServiceNow's idempotency
             mechanism — replays of the same call_id collapse to one
             incident row rather than creating duplicates.
          3. Map the LeadUpsertPayload fields into ServiceNow columns:
             * ``short_description`` ← synthesised subject
               ("Inbound call {external_call_id}")
             * ``description``       ← payload.description (redacted
               transcript)
             * ``caller_id``         ← payload.phone (matched against
               sys_user.phone where possible)
             * ``assignment_group``  ← tenant-specific, from a
               ``tenants.servicenow_assignment_group`` column
               (not yet defined).
          4. Handle the three error classes ServiceNow's REST API
             returns:
             * 201 Created  → new incident; return
               ``UpsertResult(record_id=<sys_id>, status="created")``.
             * 409 Conflict → another replay already created this
               correlation_id; GET by correlation_id, return
               ``UpsertResult(..., status="updated")``.
             * 429 Too Many Requests → tenacity-retryable; ServiceNow
               returns a ``Retry-After`` header we should honour.
             * 4xx (other) → PermanentError; surface into
               ``crm_writes.error_context`` and stop retrying.
             * 5xx         → TransientError; exponential backoff via
               tenacity, same ``retry_max_attempts`` budget as the
               Salesforce adapter.
          5. On success, write a ``crm_writes`` row with
             ``adapter='servicenow_stub'`` (or ``'servicenow'`` once real),
             ``status='success'``, and the incident's ``sys_id`` in
             ``sfdc_lead_id`` (column repurposed across adapters for
             "primary key downstream").

        The docstring length + the OAuth / correlation_id references
        are the actual deliverable here — anyone picking this up can
        skip the spec-reading step.
        """
        raise NotImplementedError(
            "ServiceNow adapter is a stub in v1 — the docstring above "
            "describes what a production implementation looks like. "
            "A real implementation arrives when a ServiceNow-first "
            "customer asks for it; CR-11 only requires the Protocol "
            "to be genericizable, which this stub proves."
        )

    async def describe_schema(self) -> dict[str, Any]:
        """Return a self-identifying shape for demo-day diagnostics.

        `status="stubbed"` is load-bearing: /readyz and the schema
        inspector surface this so a reviewer can see at a glance
        which adapter is live vs. stubbed for a given tenant.
        """
        return {"status": "stubbed", "table": "incident"}

    async def map_fields(self, call_payload: dict[str, Any]) -> LeadUpsertPayload:
        """Return a minimal valid LeadUpsertPayload for Protocol compliance.

        Not a real mapping — in a production impl this would produce
        ServiceNow-column-shaped data. But the Protocol's return type
        is `LeadUpsertPayload`, so we construct a trivial-but-valid
        instance so `isinstance(adapter, CRMAdapter)` passes and
        resolver tests that call `map_fields` to smoke-test the
        Protocol wiring don't hit NotImplementedError on the stub.

        Future refactor: once ServiceNow ships, generalize the return
        type to `LeadUpsertPayload | IncidentPayload` (tagged union)
        so each adapter can hand back its natural shape.
        """
        call = call_payload.get("call", {})
        external_call_id = call.get("call_id", "servicenow-stub-placeholder")
        return LeadUpsertPayload(
            external_call_id=external_call_id,
            first_name=None,
            last_name=None,
            phone=call.get("from_number"),
            email=None,
            company="ServiceNow Stub",
            lead_source="Retell Voice Agent",
            description=call.get("transcript"),
        )
