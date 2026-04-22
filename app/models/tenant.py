"""Pydantic models for tenants.

Kept deliberately small for C2 — just enough for the CLI and the admin
route stubs. Fuller CRUD models land in C10.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TenantCreate(BaseModel):
    """Inbound payload for POST /admin/tenants (C10) and scripts.cli create-tenant."""

    name: str = Field(..., min_length=1, max_length=200)
    profile: str = Field(..., min_length=1, max_length=50)
    phi_mode: bool = False
    active_adapter: str = "salesforce"


class Tenant(BaseModel):
    """Full tenant row as returned by admin APIs."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    profile: str
    phi_mode: bool
    active_adapter: str
    created_at: datetime


class TenantSummary(BaseModel):
    """Compact view for list-tenants CLI and list endpoints."""

    id: UUID
    name: str
    profile: str
    phi_mode: bool
