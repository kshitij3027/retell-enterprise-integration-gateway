"""FastAPI application factory.

Responsibilities as of C6:
  * configure structured logging
  * open an asyncpg pool on startup, close on shutdown
  * build the Presidio PII engines (CR-10)
  * install the TenantResolutionMiddleware (CR-5)
  * mount the /healthz + /readyz router
  * mount the /admin router (tenant + API-key management)
  * mount the /webhooks/retell router (signature verify — CR-1, CR-2)

OAuth + OTel + inbound hydration land in C7..C9.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI

from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.middleware import TenantResolutionMiddleware
from app.pii import init_pii
from app.routes import admin, health, oauth, webhooks

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks.

    Opens the asyncpg pool with reasonable defaults (min 2 / max 10) and
    tags the connection with an application_name so `pg_stat_activity`
    surfaces REIG sessions distinctly. The pool is attached to
    `app.state.db_pool` — routes read it via `app.db.get_pool`.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("startup.begin", service=settings.otel_service_name)

    pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=2,
        max_size=10,
        server_settings={"application_name": "reig-api"},
    )
    app.state.db_pool = pool
    log.info("startup.pool_ready", min_size=2, max_size=10)

    # PII engines — built once at startup so the first webhook doesn't
    # eat the spaCy model-load cost (which would blow the 2 s SLA).
    # /readyz reports false until init_pii() returns.
    init_pii()

    try:
        yield
    finally:
        log.info("shutdown.begin")
        await pool.close()
        log.info("shutdown.complete")


app = FastAPI(
    title="REIG",
    version="0.1.0",
    description="Retell Enterprise Integration Gateway",
    lifespan=lifespan,
)

# --- Middleware (order matters: outermost declared LAST) ---
# TenantResolutionMiddleware gates every non-exempt request on X-API-Key.
app.add_middleware(TenantResolutionMiddleware)

# --- Routers ---
app.include_router(health.router)
app.include_router(admin.router)
app.include_router(oauth.router)
app.include_router(webhooks.router)
