"""Tenant → CRMAdapter resolver (CR-11).

Every tenant can point at a different downstream CRM by setting
`tenants.active_adapter` to a registered name. `resolve_adapter` reads
that column, looks up the class in `REGISTRY`, and returns a fresh
instance.

Why a resolver and not `REGISTRY[settings.active_adapter](...)` inline:
  * Per-tenant override. The settings value is the GLOBAL default;
    a lending-bank tenant can flip to `servicenow_stub` without
    restarting the pod.
  * Centralized fallback. If a tenant's column is NULL / empty, we
    fall back to `settings.active_adapter` in exactly one place.
  * Future caching. The v1 impl creates a fresh instance per call
    (cheap — constructors stash args and do no I/O). A later
    optimization can introduce an `lru_cache` by `tenant_id`
    without rewriting callers.

Call site:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", ...)
            adapter = await resolve_adapter(tenant_id, conn, settings)
            # adapter is a CRMAdapter. Does not touch DB until authenticate() is called.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from adapters import REGISTRY, CRMAdapter
from app.logging import get_logger

if TYPE_CHECKING:
    import asyncpg
    import httpx

    from app.config import Settings

log = get_logger(__name__)


async def resolve_adapter(
    tenant_id: UUID,
    conn: asyncpg.Connection,
    settings: Settings,
    http_client: httpx.AsyncClient | None = None,
) -> CRMAdapter:
    """Return the CRMAdapter instance configured for this tenant.

    Reads `tenants.active_adapter` on `conn` (the caller has already
    pinned `app.tenant_id` via SET LOCAL, but `tenants` has no RLS so
    that's only needed for adjacent queries in the same tx). Falls
    back to `settings.active_adapter` when the tenant row has NULL or
    empty-string for the adapter name.

    Args:
        tenant_id:   The tenant whose `active_adapter` column to read.
        conn:        asyncpg connection. Caller owns the transaction
                     (the resolver does not open its own).
        settings:    App settings — used only for the fallback adapter
                     name, never short-circuits the tenant column.
        http_client: Optional — threaded into the adapter constructor
                     so tests can inject a respx-mocked client without
                     monkeypatching.

    Returns:
        A fresh CRMAdapter instance. `db_pool` is passed as `None`
        because the instance is constructed for this call; C7 will
        re-plumb the pool through when `authenticate`/`upsert_record`
        actually need DB access.

    Raises:
        ValueError: the resolved adapter name isn't in REGISTRY (misspelled
                    column value or unloaded concrete class).
    """
    row = await conn.fetchrow(
        "SELECT active_adapter FROM tenants WHERE id = $1",
        tenant_id,
    )

    # Default chain: tenant column → settings.active_adapter.
    # `NULLIF`-style handling in Python since NULL / "" / missing row
    # should all produce the same fallback.
    name: str | None = None
    if row is not None:
        raw = row["active_adapter"]
        if isinstance(raw, str) and raw.strip():
            name = raw

    if name is None:
        name = settings.active_adapter
        log.debug(
            "adapter_resolver.fallback_to_settings",
            tenant_id=str(tenant_id),
            adapter=name,
        )

    factory = REGISTRY.get(name)
    if factory is None:
        # Fail loudly — a typo in tenants.active_adapter should not
        # silently resolve to a different adapter.
        raise ValueError(
            f"Unknown adapter: {name!r}. Registered: {sorted(REGISTRY.keys())!r}"
        )

    log.info(
        "adapter.resolved",
        tenant_id=str(tenant_id),
        name=name,
    )

    # `factory` is an AdapterFactory (see adapters/__init__.py) — its
    # `__call__` shape matches every concrete adapter's `__init__` exactly,
    # so this call is fully type-checked.
    # db_pool=None: the adapter is instantiated cheap; C7 will flow a
    # live pool through the constructor once `authenticate` / `upsert_record`
    # actually need to read credentials.
    return factory(
        tenant_id=tenant_id,
        db_pool=None,
        settings=settings,
        http_client=http_client,
    )
