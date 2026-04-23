"""ServiceNow stub-specific tests (CR-11).

The ServiceNow adapter's demo-day deliverable is:
  1. `upsert_record` raises NotImplementedError (we don't ship a real impl).
  2. The docstring on that method is long and specific enough that
     anyone picking this up has a spec instead of a blank function.
  3. `describe_schema`, `authenticate`, and `map_fields` satisfy the
     Protocol (no raise) so resolver / Protocol tests don't hit the
     NotImplementedError when smoke-testing the class.

These tests enforce (1) + (2) explicitly; (3) is covered in
test_adapter_protocol.py.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from adapters.base import LeadUpsertPayload
from adapters.servicenow import ServiceNowAdapter
from app.config import get_settings


@pytest.mark.asyncio
async def test_upsert_raises_not_implemented() -> None:
    """upsert_record MUST raise NotImplementedError in the stub."""
    settings = get_settings()
    adapter = ServiceNowAdapter(uuid4(), None, settings)

    payload = LeadUpsertPayload(
        external_call_id="call_abc123",
        first_name=None,
        last_name=None,
        phone=None,
        email=None,
        company="Acme",
        lead_source="Retell Voice Agent",
        description=None,
    )

    with pytest.raises(NotImplementedError):
        await adapter.upsert_record(payload)


def test_docstring_describes_production_implementation() -> None:
    """The stub's docstring is the deliverable — enforce it's present + specific.

    Reads the raw function's __doc__ (unbounded to a method call so we
    can assert on it without instantiating). Checks:
      * > 100 chars (not a placeholder)
      * Contains "OAuth" (auth mechanism documented)
      * Contains "correlation" (idempotency mechanism documented)
    """
    doc = ServiceNowAdapter.upsert_record.__doc__
    assert doc is not None, "upsert_record must have a docstring"
    assert len(doc) >= 100, f"docstring too short: {len(doc)} chars"
    assert "OAuth" in doc, "docstring must mention OAuth auth mechanism"
    # "correlation" covers both "correlation_id" and "x-correlation-id"
    # forms — either is acceptable.
    assert "correlation" in doc.lower(), (
        "docstring must describe the x-correlation-id idempotency mechanism"
    )


@pytest.mark.asyncio
async def test_authenticate_no_op_does_not_raise() -> None:
    """Stub's authenticate returns None without side effects — matches Protocol."""
    settings = get_settings()
    adapter = ServiceNowAdapter(uuid4(), None, settings)
    result = await adapter.authenticate()
    assert result is None


@pytest.mark.asyncio
async def test_map_fields_returns_valid_payload() -> None:
    """Stub map_fields returns a minimal but validatable LeadUpsertPayload."""
    settings = get_settings()
    adapter = ServiceNowAdapter(uuid4(), None, settings)

    payload = await adapter.map_fields(
        {"call": {"call_id": "call_xyz", "from_number": "+15555551234"}}
    )
    assert isinstance(payload, LeadUpsertPayload)
    assert payload.external_call_id == "call_xyz"
    assert payload.phone == "+15555551234"
