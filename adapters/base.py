"""Canonical Protocol + payload shapes for every downstream CRM adapter (CR-11).

The middleware (`app/call_pipeline.py`, `app/adapter_resolver.py`) never imports
a concrete adapter. It speaks exclusively to `CRMAdapter`, a typing.Protocol
that describes the four methods every adapter MUST implement:

  * `authenticate()`    — make sure the adapter has a usable access token.
                          For Salesforce this refreshes OAuth; for
                          ServiceNow it would refresh client-credentials.
  * `upsert_record(p)`  — idempotent write of a single call's lead/incident
                          into the downstream system. Keyed on
                          `LeadUpsertPayload.external_call_id` so replayed
                          webhooks land exactly once.
  * `describe_schema()` — cheap introspection for /readyz and demo-day
                          dashboards: what object, which external-id field,
                          which API version.
  * `map_fields(d)`     — pure function — Retell webhook dict →
                          `LeadUpsertPayload`. Pulled out of `upsert_record`
                          so C6 can redact the transcript without teaching
                          the adapter about Presidio.

Why Protocol (not ABC):
  * Protocol + `@runtime_checkable` gives us both static mypy --strict
    enforcement ("does SalesforceAdapter have the right methods with the
    right types?") AND a runtime `isinstance(adapter, CRMAdapter)` check
    that the registry / resolver can use as a smoke test.
  * ABCs force inheritance — adapters would be bound to our class tree,
    which makes dropping in a third-party SDK adapter harder. Protocol
    is structural: any class that happens to expose the four methods
    with matching signatures is a CRMAdapter.

Payload shapes are Pydantic v2 with `extra="forbid"` so an adapter cannot
accidentally leak an unknown field through the typed surface — you have to
extend the model on purpose. `mypy --strict adapters/` is the CI gate.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class LeadUpsertPayload(BaseModel):
    """Normalized payload shape for a `Lead`/`Incident`-style upsert.

    `external_call_id` is the idempotency key — Salesforce upserts PATCH
    against `/sobjects/Lead/External_Call_Id__c/{external_call_id}`, and
    ServiceNow uses it as the `x-correlation-id` header so replayed
    webhooks collapse to exactly one downstream row per call_id.

    Every other field is optional because Retell's `call_analyzed`
    payload is sparse on early-arriving or partial events. Adapters
    MAY fill gaps with sensible defaults (Salesforce requires
    `Company` on Lead, for instance — `SalesforceAdapter.map_fields`
    sets it to `"Unknown (inbound call)"`).

    `extra="forbid"` means typos in call sites fail loudly at model
    construction rather than silently dropping data somewhere
    downstream of the adapter.
    """

    model_config = ConfigDict(extra="forbid")

    external_call_id: str
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    email: str | None = None
    company: str
    lead_source: str
    description: str | None = None


class UpsertResult(BaseModel):
    """Outcome of a successful `upsert_record` call.

    `status` is a two-value Literal so callers can cleanly disambiguate
    the first-time write from a replay that just touched an existing
    row. Salesforce's PATCH returns 201 (created) vs. 204 (updated) —
    `SalesforceAdapter.upsert_record` maps those into this shape.
    """

    model_config = ConfigDict(extra="forbid")

    record_id: str
    status: Literal["created", "updated"]


class LookupResult(BaseModel):
    """Result of a "does this contact already exist?" probe.

    Unused by the middleware in C5 — the write path is upsert-on-
    external-id, so we never read-then-write. Present in the type
    library so `describe_schema` demos and future "enrich the CRM
    row with historical data" flows (Extended Feature B) have a
    place to hand back normalized shape data.
    """

    model_config = ConfigDict(extra="forbid")

    record_id: str
    first_name: str | None = None
    last_name: str | None = None
    last_activity_date: date | None = None
    open_cases: int = 0
    account_status: str | None = None


class ContactPayload(BaseModel):
    """Minimal contact shape for "create-or-attach" style flows.

    Present so a future Salesforce `Contact`/`Account` hierarchy write
    (as opposed to the Lead write we ship in C7) can go through the
    same Protocol — `upsert_record` could be overloaded or a sibling
    method added without rewriting the map/auth plumbing.
    """

    model_config = ConfigDict(extra="forbid")

    phone: str
    first_name: str | None = None
    last_name: str | None = None


class CallActivityPayload(BaseModel):
    """Payload for writing a `Task` / `CallActivity` log row.

    Unused in C5 — `upsert_record` writes directly to `Lead`. We keep
    this in the typed surface because the JD calls out "call activity"
    as a first-class concept and the Extended Feature area adds a
    follow-on "log the call as a Task against the Lead" step that
    would use this shape.
    """

    model_config = ConfigDict(extra="forbid")

    external_call_id: str
    subject: str
    description: str | None = None
    call_duration_seconds: int | None = None


@runtime_checkable
class CRMAdapter(Protocol):
    """Structural Protocol every downstream CRM adapter must satisfy.

    `@runtime_checkable` enables `isinstance(x, CRMAdapter)` at runtime —
    the resolver and its tests use that to verify the registry is wired
    correctly without having to import every concrete class.

    The signatures here are the exact contract. Any concrete adapter
    whose methods don't match causes `mypy --strict adapters/` to fail
    loudly.
    """

    async def authenticate(self) -> None:
        """Refresh / establish the adapter's downstream credentials.

        Implementations should be idempotent — safe to call before every
        `upsert_record` — and cheap on the happy path (no network round
        trip if the cached token is still fresh).
        """
        ...

    async def upsert_record(self, payload: LeadUpsertPayload) -> UpsertResult:
        """Write one call's normalized payload into the downstream system.

        MUST be idempotent on `payload.external_call_id`: firing the same
        payload N times produces exactly one row downstream. Salesforce
        achieves this via External_Call_Id__c PATCH; ServiceNow via
        `x-correlation-id` on the table POST.
        """
        ...

    async def describe_schema(self) -> dict[str, Any]:
        """Return a small dict describing the target object + API version.

        Cheap — no network call. Powers /readyz smoke tests and demo-day
        wall displays. Shape is adapter-specific; treat as opaque.
        """
        ...

    async def map_fields(self, call_payload: dict[str, Any]) -> LeadUpsertPayload:
        """Translate a raw Retell `call_analyzed` dict into LeadUpsertPayload.

        Pure function — no network, no DB. Split out of `upsert_record`
        so C6's PII redaction can run BETWEEN `map_fields` and
        `upsert_record` without the adapter having to know about
        Presidio.
        """
        ...
