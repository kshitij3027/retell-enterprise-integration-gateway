"""CRMAdapter Protocol compliance tests (CR-11).

These tests don't touch the DB or the network — they prove that:

1. Every concrete adapter structurally satisfies `CRMAdapter` at
   runtime via `isinstance(..., CRMAdapter)` (which works because
   the Protocol is `@runtime_checkable`).
2. The REGISTRY is populated for both known adapters.
3. `describe_schema` returns a plain dict on both adapters.
4. `SalesforceAdapter.map_fields` produces a valid LeadUpsertPayload
   from a realistic Retell `call_analyzed` dict.

No asyncpg, no httpx, no DB — construction with `db_pool=None,
http_client=None` is a supported C5 shape.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from adapters import REGISTRY, CRMAdapter, LeadUpsertPayload
from adapters.salesforce import SalesforceAdapter
from adapters.servicenow import ServiceNowAdapter
from app.config import get_settings


def test_registry_has_both_adapters() -> None:
    """REGISTRY contains the two names the middleware knows about."""
    assert set(REGISTRY.keys()) >= {"salesforce", "servicenow_stub"}
    assert REGISTRY["salesforce"] is SalesforceAdapter
    assert REGISTRY["servicenow_stub"] is ServiceNowAdapter


def test_salesforce_satisfies_protocol() -> None:
    """SalesforceAdapter passes isinstance(CRMAdapter) at runtime."""
    settings = get_settings()
    adapter = SalesforceAdapter(uuid4(), None, settings)
    assert isinstance(adapter, CRMAdapter)


def test_servicenow_satisfies_protocol() -> None:
    """ServiceNowAdapter passes isinstance(CRMAdapter) at runtime."""
    settings = get_settings()
    adapter = ServiceNowAdapter(uuid4(), None, settings)
    assert isinstance(adapter, CRMAdapter)


@pytest.mark.asyncio
async def test_salesforce_describe_schema_returns_dict() -> None:
    """Salesforce describe_schema returns a Lead / External_Call_Id__c shape."""
    settings = get_settings()
    adapter = SalesforceAdapter(uuid4(), None, settings)
    schema = await adapter.describe_schema()
    assert isinstance(schema, dict)
    assert schema["object"] == "Lead"
    assert schema["external_id_field"] == "External_Call_Id__c"
    assert schema["api_version"] == settings.sfdc_api_version


@pytest.mark.asyncio
async def test_servicenow_describe_schema_returns_dict() -> None:
    """ServiceNow describe_schema returns the stubbed/incident shape."""
    settings = get_settings()
    adapter = ServiceNowAdapter(uuid4(), None, settings)
    schema = await adapter.describe_schema()
    assert isinstance(schema, dict)
    assert schema["status"] == "stubbed"
    assert schema["table"] == "incident"


@pytest.mark.asyncio
async def test_salesforce_map_fields_produces_valid_payload() -> None:
    """A realistic Retell call_analyzed dict → a fully-populated LeadUpsertPayload.

    Asserts on every field the adapter claims to populate so a future
    refactor that drops one of them fails loudly.
    """
    settings = get_settings()
    adapter = SalesforceAdapter(uuid4(), None, settings)

    sample_call_analyzed = {
        "event": "call_analyzed",
        "call": {
            "call_id": "call_abc123",
            "from_number": "+14155551234",
            "to_number": "+14085550000",
            "agent_id": "agent_xyz",
            "transcript": "Hi this is John. My SSN is 123-45-6789.",
        },
    }

    payload = await adapter.map_fields(sample_call_analyzed)

    assert isinstance(payload, LeadUpsertPayload)
    assert payload.external_call_id == "call_abc123"
    assert payload.phone == "+14155551234"
    assert payload.email is None
    assert payload.company == "Unknown (inbound call)"
    assert payload.lead_source == "Retell Voice Agent"
    # C5 does NOT redact; C6 will wrap map_fields with Presidio. Assert
    # the raw transcript flows through so C6's test can flip the polarity.
    assert payload.description is not None
    assert "123-45-6789" in payload.description


@pytest.mark.asyncio
async def test_salesforce_map_fields_missing_call_id_raises() -> None:
    """call_id is required — missing key should fail loudly."""
    settings = get_settings()
    adapter = SalesforceAdapter(uuid4(), None, settings)

    with pytest.raises(KeyError):
        await adapter.map_fields({"call": {"from_number": "+14155551234"}})


@pytest.mark.asyncio
async def test_salesforce_map_fields_tolerates_missing_optional_fields() -> None:
    """Only call_id is required; everything else flows through as None."""
    settings = get_settings()
    adapter = SalesforceAdapter(uuid4(), None, settings)

    payload = await adapter.map_fields({"call": {"call_id": "call_min"}})
    assert payload.external_call_id == "call_min"
    assert payload.phone is None
    assert payload.description is None
    assert payload.company == "Unknown (inbound call)"
