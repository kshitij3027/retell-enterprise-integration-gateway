"""Admin routes — tenant + API-key + audit read-only CRUD.

C10 surface:
  * POST /admin/tenants/{tenant_id}/keys        — issue a new API key.
  * GET  /admin/tenants/{tenant_id}             — fetch tenant metadata.
  * GET  /admin/tenants/{tenant_id}/calls       — recent calls for this tenant.
  * GET  /admin/tenants/{tenant_id}/audit       — recent audit rows.
  * DELETE /admin/tenants/{tenant_id}/keys/{id} — revoke a key.

Every endpoint sits behind the TenantResolutionMiddleware, so
`request.state.tenant_id` is pinned to the authenticated tenant and
the path-param {tenant_id} is middleware-enforced to match. We don't
re-check it here.

Full tenant CRUD (POST /admin/tenants, GET /admin/tenants) is stubbed
out — creating a tenant is a CLI operation (`scripts.cli create-tenant`)
because the "who issues the first key" question is better solved
inside the container.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.auth import generate_key
from app.config import get_settings
from app.db import get_db
from app.logging import get_logger
from app.models.api_key import ApiKeyIssued

if TYPE_CHECKING:
    import asyncpg

log = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Key issuance + revocation (CR-6)
# ---------------------------------------------------------------------------
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
    """Mint a fresh API key for the authenticated tenant."""
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


@router.delete(
    "/tenants/{tenant_id}/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_tenant_key(
    tenant_id: UUID,
    key_id: UUID,
    conn: asyncpg.Connection = Depends(get_db),
) -> Response:
    """Delete one api_keys row. RLS ensures cross-tenant calls find nothing."""
    result = await conn.execute(
        "DELETE FROM api_keys WHERE id = $1 AND tenant_id = $2",
        key_id,
        tenant_id,
    )
    try:
        deleted = int(result.split()[-1])
    except Exception:  # noqa: BLE001 — defensive
        deleted = 0
    if deleted == 0:
        raise HTTPException(status_code=404, detail="key not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Tenant metadata + activity
# ---------------------------------------------------------------------------
@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: UUID,
    conn: asyncpg.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Return tenant name + profile + phi_mode."""
    row = await conn.fetchrow(
        "SELECT id, name, profile, phi_mode, active_adapter, created_at "
        "FROM tenants WHERE id = $1",
        tenant_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "profile": row["profile"],
        "phi_mode": row["phi_mode"],
        "active_adapter": row["active_adapter"],
        "created_at": row["created_at"].isoformat(),
    }


@router.get("/tenants/{tenant_id}/calls")
async def list_calls(
    tenant_id: UUID,
    limit: int = Query(default=50, ge=1, le=500),
    conn: asyncpg.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Recent `calls` rows for the authenticated tenant."""
    rows = await conn.fetch(
        "SELECT id, call_id, metadata, created_at FROM calls "
        "WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT $2",
        tenant_id,
        limit,
    )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "call_id": r["call_id"],
                "metadata": r["metadata"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@router.get("/tenants/{tenant_id}/audit")
async def list_audit(
    tenant_id: UUID,
    event_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    conn: asyncpg.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Recent audit_log rows, optionally filtered by event_type."""
    if event_type:
        rows = await conn.fetch(
            "SELECT event_type, call_id, payload, trace_id, source_ip, created_at "
            "FROM audit_log WHERE tenant_id = $1 AND event_type = $2 "
            "ORDER BY created_at DESC LIMIT $3",
            tenant_id,
            event_type,
            limit,
        )
    else:
        rows = await conn.fetch(
            "SELECT event_type, call_id, payload, trace_id, source_ip, created_at "
            "FROM audit_log WHERE tenant_id = $1 "
            "ORDER BY created_at DESC LIMIT $2",
            tenant_id,
            limit,
        )
    return {
        "items": [
            {
                "event_type": r["event_type"],
                "call_id": r["call_id"],
                "payload": r["payload"],
                "trace_id": r["trace_id"],
                "source_ip": str(r["source_ip"]) if r["source_ip"] is not None else None,
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Full-list stubs — intentionally declined (use the CLI instead).
# ---------------------------------------------------------------------------
@router.get("/tenants", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def list_tenants_stub() -> dict[str, str]:
    """Cross-tenant listing isn't exposed over the authenticated API."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="cross-tenant listing is CLI-only; use scripts.cli list-tenants",
    )


@router.post("/tenants", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def create_tenant_stub() -> dict[str, str]:
    """Tenant creation is CLI-only — the HTTP API requires a tenant already."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="tenant creation is CLI-only; use scripts.cli create-tenant",
    )
