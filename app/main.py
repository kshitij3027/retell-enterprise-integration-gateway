"""FastAPI application factory.

Responsibilities in C1:
  * configure structured logging
  * open an asyncpg pool on startup, close on shutdown
  * mount the /healthz + /readyz router

Webhook, admin, and tool routes are added in C3+.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg
from fastapi import FastAPI

from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.routes import health

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

# --- Routers ---
app.include_router(health.router)
