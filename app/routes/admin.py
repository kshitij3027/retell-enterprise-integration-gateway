"""Admin routes — tenant + API-key management.

C2 scope:
  * POST /admin/tenants/{tenant_id}/keys — issue an API key. Returns the
    raw key EXACTLY ONCE in the response body + an X-REIG-Key-Warning header.
  * GET  /admin/tenants               — 501 (full CRUD lands in C10)
  * POST /admin/tenants               — 501 (full CRUD lands in C10)
  * GET  /admin/tenants/{tenant_id}   — 501

All of these sit behind the TenantResolutionMiddleware, so by the time a
handler runs `request.state.tenant_id` is already pinned to the authenticated
tenant. The path-level {tenant_id} equality check is also enforced by the
middleware — we don't need to re-check it here.

For C2's bootstrap-then-rotate story there's a chicken-and-egg question:
how do you create the FIRST key? Answer: scripts/cli.py — it runs outside
the HTTP layer with superuser credentials (via `docker compose run`) and
inserts the very first (tenant, key) pair directly into the DB.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.auth import generate_key
from app.config import get_settings
from app.db import get_db
from app.logging import get_logger
from app.models.api_key import ApiKeyIssued

if TYPE_CHECKING:
    import asyncpg

log = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


@router.post(
    "/tenants/{tenant_id}/keys",
    response_model=ApiKeyIssued,
    status_code=status.HTTP_201_CREATED,
)
async def issue_tenant_key(
    tenant_id: UUID,
    response: Response,
    conn: asyncpg.Connection = Depends(get_db),
) -> ApiKeyIssued:
    """Mint a fresh API key for the authenticated tenant.

    The middleware has already verified the caller's X-API-Key matches the
    path-param tenant — so here we just insert the new hash and return the
    raw key ONCE. Store it immediately; the raw bytes are never persisted.

    Returns the `ApiKeyIssued` model; callers should also respect the
    `X-REIG-Key-Warning` response header.
    """
    settings = get_settings()
    raw, stored_hash = generate_key(prefix=settings.tenant_api_key_prefix)

    try:
        await conn.execute(
            "INSERT INTO api_keys (tenant_id, key_hash, key_prefix) "
            "VALUES ($1, $2, $3)",
            tenant_id,
            stored_hash,
            settings.tenant_api_key_prefix,
        )
    except Exception as exc:  # noqa: BLE001 — bubble up as 500 w/ structured log
        log.error("admin.issue_key.failed", tenant_id=str(tenant_id), error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to issue key",
        ) from exc

    response.headers["X-REIG-Key-Warning"] = "store-immediately"
    log.info(
        "admin.issue_key.ok",
        tenant_id=str(tenant_id),
        key_prefix=settings.tenant_api_key_prefix,
    )
    return ApiKeyIssued(
        key=raw,
        key_prefix=settings.tenant_api_key_prefix,
        tenant_id=tenant_id,
    )


# ---------------------------------------------------------------------------
# Stubs — full tenant CRUD lands in C10.
# ---------------------------------------------------------------------------
@router.get("/tenants", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def list_tenants_stub() -> dict[str, str]:
    """Placeholder; implemented in C10."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="list-tenants is implemented in C10; use scripts.cli list-tenants",
    )


@router.post("/tenants", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def create_tenant_stub() -> dict[str, str]:
    """Placeholder; implemented in C10."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="create-tenant is implemented in C10; use scripts.cli create-tenant",
    )


@router.get("/tenants/{tenant_id}", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def get_tenant_stub(tenant_id: UUID) -> dict[str, str]:
    """Placeholder; implemented in C10."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="get-tenant is implemented in C10",
    )
