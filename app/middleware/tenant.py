"""Tenant resolution middleware (CR-5 in practice).

Every non-public request must carry an `X-API-Key` header. This middleware:
  1. Exempts public paths (health probes, Retell webhooks, OAuth callback).
     Webhooks authenticate via HMAC in the route handler; the OAuth callback
     authenticates via a signed `state` param.
  2. Reads `X-API-Key` and SHA-256 hashes it.
  3. Looks up the matching api_keys row via a short-lived bootstrap transaction
     (sets `app.bootstrap='true'` inside the tx so the RLS policy admits the
     SELECT without a tenant context).
  4. Verifies the hash with `hmac.compare_digest` (even though the DB lookup
     is already hash-based — belt and braces against future schema changes).
  5. Stashes `request.state.tenant_id` so `get_db()` can pin RLS downstream.
  6. If the path contains `{tenant_id}`, it MUST equal the authenticated
     tenant's UUID — otherwise 403 (cross-tenant access attempt).

Implemented as a Starlette `BaseHTTPMiddleware` subclass so it sees the raw
request object (we need `request.state`). FastAPI's dependency system fires
*after* middleware, so `get_db()` can trust `request.state.tenant_id` exists
on any non-exempt route.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.auth import hash_key, verify_key
from app.logging import get_logger

log = get_logger(__name__)

# Paths that skip API-key auth entirely. These authenticate by other means
# (HMAC signature for webhooks, signed state for OAuth callback) or aren't
# security-sensitive (health probes).
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/readyz",
    "/admin/oauth/",   # callback + initiate
    "/webhooks/retell/",
    "/docs",
    "/openapi.json",
    "/redoc",
)

# Regex to find `/admin/tenants/<uuid>` in the path. Matches v1 UUIDs (hex-dash).
# Used only to extract the path-param tenant_id for the cross-tenant check.
_TENANT_PATH_RE = re.compile(
    r"/admin/tenants/(?P<tid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _is_exempt(path: str) -> bool:
    """True if `path` should skip API-key auth."""
    return any(path == p or path.startswith(p) for p in _EXEMPT_PREFIXES)


class TenantResolutionMiddleware(BaseHTTPMiddleware):
    """Maps X-API-Key → request.state.tenant_id, or 401/403.

    This runs before any route handler. Route handlers and their `get_db`
    dependency trust that `request.state.tenant_id` is populated on every
    non-exempt request.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path

        if _is_exempt(path):
            return await call_next(request)

        raw_key = request.headers.get("X-API-Key")
        if not raw_key:
            log.info("auth.missing_api_key", path=path)
            return JSONResponse(
                {"detail": "missing X-API-Key header"},
                status_code=401,
            )

        key_hash = hash_key(raw_key)

        # Bootstrap lookup: use the pool directly so we don't depend on
        # FastAPI's DI machinery (middleware runs before dependencies).
        pool = request.app.state.db_pool
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.bootstrap', 'true', true)"
                )
                row = await conn.fetchrow(
                    "SELECT tenant_id, key_hash FROM api_keys "
                    "WHERE key_hash = $1 LIMIT 1",
                    key_hash,
                )

        if row is None:
            log.info("auth.key_not_found", path=path, key_prefix=raw_key[:6])
            return JSONResponse(
                {"detail": "invalid API key"},
                status_code=401,
            )

        # compare_digest against the stored hash — defense in depth.
        if not verify_key(raw_key, row["key_hash"]):
            log.warning("auth.compare_digest_failed", path=path)
            return JSONResponse(
                {"detail": "invalid API key"},
                status_code=401,
            )

        authed_tenant_id: UUID = row["tenant_id"]
        request.state.tenant_id = authed_tenant_id

        # Cross-tenant guard — any `/admin/tenants/<uuid>/...` path-param
        # must equal the authenticated tenant or we 403.
        m = _TENANT_PATH_RE.search(path)
        if m is not None:
            try:
                path_tid = UUID(m.group("tid"))
            except ValueError:
                # Extremely unlikely because the regex already constrained
                # the shape, but belt-and-braces for malformed UUIDs.
                return JSONResponse(
                    {"detail": "invalid tenant uuid in path"},
                    status_code=400,
                )
            if path_tid != authed_tenant_id:
                log.warning(
                    "auth.tenant_mismatch",
                    path=path,
                    authed=str(authed_tenant_id),
                    requested=str(path_tid),
                )
                return JSONResponse(
                    {"detail": "tenant mismatch"},
                    status_code=403,
                )

        return await call_next(request)
