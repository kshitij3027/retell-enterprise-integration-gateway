"""Salesforce CRM adapter — scaffolding for C5, filled in C7.

C5 surface (this file):
  * `SalesforceAdapter` registered under `"salesforce"`.
  * `map_fields(...)` FULLY implemented — pure Retell→Lead translation.
  * `describe_schema(...)` returns the target object + API version.
  * `authenticate(...)` is a `pass`-stub with a TODO(C7) pointer.
  * `upsert_record(...)` raises `NotImplementedError` with a pointer to C7.

Why ship this half-baked now: the Protocol contract (CR-11) is a hard gate
for demo-day — a reviewer should be able to see two adapters satisfy the
same interface and one is swapped via `tenants.active_adapter` — but the
real SFDC upsert involves OAuth refresh + pgcrypto-encrypted refresh tokens
+ tenacity retries (CR-7, CR-8, CR-9) which all land together in C7. Shipping
the skeleton now gives the pipeline a live wire to `map_fields` (no
NotImplementedError on the request path), and C7 becomes a contained diff
in this one file.

The C5 live pipeline in `app/call_pipeline.py` intentionally calls only
`authenticate` (no-op) and `map_fields` — never `upsert_record` — so no
BackgroundTask crashes on NotImplementedError. Grep for `TODO(C7)` to find
the exact line that flips in C7.
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


@register("salesforce")
class SalesforceAdapter:
    """Salesforce REST adapter — C5 skeleton, C7 finishes it.

    Construction is deliberately permissive on dependencies:
      * `db_pool=None`   — adapter_resolver instantiates without a pool
                           because C5's pipeline only calls `map_fields`
                           (pure) + `authenticate` (stub). C7 will pass
                           a real pool so `authenticate` can read +
                           update cached access tokens from `credentials`.
      * `http_client=None` — same rationale. C7 wires an `httpx.AsyncClient`
                            for the OAuth refresh + PATCH calls.

    `authenticate` and `upsert_record` MUST raise loudly if the dependency
    they need is absent at call time — C5 doesn't call them on the live
    path, but a future caller that forgets to pass the pool should see
    a clear error instead of a `AttributeError: 'NoneType' has no ...`.
    """

    def __init__(
        self,
        tenant_id: UUID,
        db_pool: asyncpg.Pool | None,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Stash construction args for later use in authenticate / upsert_record.

        Args:
            tenant_id:   Owner of every downstream write this adapter
                         instance produces. Also the RLS key for
                         `credentials` reads in C7.
            db_pool:     asyncpg pool used to read/write `credentials` rows.
                         `None` is allowed at construction so the resolver
                         can hand back an instance even when the caller
                         only needs `map_fields` or `describe_schema`.
                         C7's `authenticate` will assert not-None.
            settings:    App `Settings` — we read `sfdc_api_version`,
                         `sfdc_instance_url`, `sfdc_client_id` /
                         `sfdc_client_secret` from this in C7.
            http_client: Optional injected httpx client (for respx mocking
                         in tests). `None` means "build one on demand in
                         C7"; tests will hand in a mocked one.
        """
        self.tenant_id = tenant_id
        self.db_pool = db_pool
        self.settings = settings
        self.http_client = http_client

    async def authenticate(self) -> None:
        """No-op stub for C5. C7 fills in OAuth access-token refresh.

        Contract: idempotent, cheap on the happy path. Real impl will:
          1. `SELECT access_token_cached, access_token_expires_at,
              decrypt_refresh_token(refresh_token_encrypted), instance_url
              FROM credentials WHERE tenant_id=$1 AND adapter='salesforce'`.
          2. If cache is fresh (>60s to expiry), return immediately.
          3. Otherwise POST to `/services/oauth2/token` with
             `grant_type=refresh_token` and persist the new pair.

        C5 keeps this as `pass` so the pipeline can call it without a
        live Salesforce sandbox.
        """
        # TODO(C7): OAuth refresh + credentials row update.
        return None

    async def upsert_record(self, payload: LeadUpsertPayload) -> UpsertResult:
        """Unimplemented in C5 — raised intentionally.

        C7 will implement this as a PATCH to
        `{instance_url}/services/data/{api_version}/sobjects/Lead/External_Call_Id__c/{payload.external_call_id}`
        with a JSON body derived from `payload.model_dump(exclude={"external_call_id"})`.
        Response handling: 201→created, 204→updated, 401 INVALID_SESSION_ID→
        refresh+retry once, 4xx→PermanentError, 429/5xx→TransientError+tenacity.

        The live C5 pipeline does NOT call this method — see `TODO(C7)` in
        `app/call_pipeline.py`. Tests do call it (to verify the placeholder)
        and assert on the exception + message.
        """
        raise NotImplementedError(
            "Salesforce upsert arrives in C7 "
            "(OAuth + PATCH /sobjects/Lead/External_Call_Id__c/{id})."
        )

    async def describe_schema(self) -> dict[str, Any]:
        """Return the target object + external-id field + API version.

        Pure — no DB, no network. /readyz and the demo-day schema
        inspector pull this.
        """
        return {
            "object": "Lead",
            "external_id_field": "External_Call_Id__c",
            "api_version": self.settings.sfdc_api_version,
        }

    async def map_fields(self, call_payload: dict[str, Any]) -> LeadUpsertPayload:
        """Translate a raw Retell `call_analyzed` dict into a LeadUpsertPayload.

        Input shape (loose — Retell adds fields over time):
            {
                "event": "call_analyzed",
                "call": {
                    "call_id":     "<required>",
                    "from_number": "+14155551234" | None,
                    "to_number":   "+14085550000" | None,
                    "agent_id":    "..."          | None,
                    "transcript":  "..."          | None,
                    ...
                }
            }

        Mapping rules:
          * `external_call_id` ← `call.call_id`. REQUIRED. Raises
            KeyError if missing (caller's job to validate upstream; the
            webhook router already enforces it via Pydantic).
          * `phone` ← `call.from_number`. None when the call hasn't
            been attributed yet.
          * `first_name` / `last_name` ← Retell v1 doesn't carry these,
            so both are None in C5. Future enhancement: derive from
            `call.analysis.custom_analysis_data`.
          * `email` ← Retell v1 payloads don't carry email; None.
          * `company` ← hard-coded `"Unknown (inbound call)"` because
            Salesforce rejects a Lead insert without `Company`. This
            default is intentionally ugly — a downstream enrichment
            step (Extended Feature B) would overwrite it.
          * `lead_source` ← hard-coded `"Retell Voice Agent"` so every
            Lead carries attribution back to this pipeline.
          * `description` ← `call.transcript`. **Not yet redacted** —
            C6 wraps `map_fields` with a Presidio pass before any
            `upsert_record` can run. Keeping `map_fields` PII-unaware
            means the adapter doesn't need to care about Presidio, and
            C6 becomes a thin wrapper in `app/call_pipeline.py`.

        Errors:
            KeyError: if `call_payload["call"]["call_id"]` is missing.

        Returns:
            LeadUpsertPayload — validated via pydantic, safe to hand
            straight into `upsert_record` (once C7 implements it).
        """
        call = call_payload.get("call", {})
        # external_call_id is required — KeyError on missing is the right
        # failure mode (upstream middleware guarantees it's present).
        external_call_id = call["call_id"]

        from_number = call.get("from_number")
        transcript = call.get("transcript")

        return LeadUpsertPayload(
            external_call_id=external_call_id,
            first_name=None,
            last_name=None,
            phone=from_number,
            email=None,
            company="Unknown (inbound call)",
            lead_source="Retell Voice Agent",
            description=transcript,
        )
