"""Liveness and readiness endpoints (EB-3).

  * /healthz — cheap liveness: always 200 if the process is serving.
  * /readyz  — deep readiness: must be able to SELECT 1 on the DB pool.

Kubernetes / Docker Swarm / compose healthchecks typically hit /healthz;
load balancers and CI smoke checks hit /readyz to confirm the stack is
actually wired up (not just running).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Response, status

from app.db import get_pool
from app.logging import get_logger

if TYPE_CHECKING:
    from asyncpg.pool import Pool

log = get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/healthz", status_code=status.HTTP_200_OK)
async def healthz() -> dict[str, str]:
    """Process is alive and serving. No I/O."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(
    response: Response,
    pool: Pool = Depends(get_pool),
) -> dict[str, str]:
    """DB pool is ready. Returns 503 if we can't SELECT 1."""
    try:
        async with pool.acquire() as conn:
            value = await conn.fetchval("SELECT 1")
            if value != 1:
                raise RuntimeError(f"SELECT 1 returned {value!r}")
        return {"status": "ok", "db": "ok"}
    except Exception as exc:  # noqa: BLE001 — intentionally broad on readiness
        log.warning("readyz.db_unreachable", error=str(exc))
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "db": "unreachable"}
