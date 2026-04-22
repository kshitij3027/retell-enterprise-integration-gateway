"""Pydantic models for API keys.

Three distinct shapes:
  * ApiKeyCreate  — inbound to POST /admin/tenants/{id}/keys. Empty today;
    future C10 may add label/expires_at/scopes.
  * ApiKeyIssued  — outbound ONE-TIME response body. Contains the raw key.
    Never persisted, never logged.
  * ApiKeyStored  — the sanitised representation safe for listing: hash +
    metadata, NO raw key. What lives in the DB (modulo row id).
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApiKeyCreate(BaseModel):
    """Inbound POST body for key issuance. Intentionally empty in C2."""

    # Placeholder kept for forward-compat; admins can add label/scopes in C10.
    model_config = ConfigDict(extra="forbid")


class ApiKeyIssued(BaseModel):
    """Response body returned exactly once at key-issuance time.

    `key` is the raw, usable API key (caller must store immediately).
    `warning` is echoed in both body and the X-REIG-Key-Warning header.
    """

    key: str = Field(..., description="Raw API key. Store immediately; not retrievable.")
    key_prefix: str = Field(..., description="Indexable prefix (e.g. 'reig_').")
    tenant_id: UUID
    warning: str = "store immediately; cannot be retrieved again"


class ApiKeyStored(BaseModel):
    """Sanitised row for list endpoints. Never contains the raw key."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    key_hash: str
    key_prefix: str
    created_at: datetime
