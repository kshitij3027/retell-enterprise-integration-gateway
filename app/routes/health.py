"""Liveness and readiness endpoints (EB-3).

  * /healthz — cheap liveness: always 200 if the process is serving.
  * /readyz  — deep readiness: DB SELECT 1 + Presidio engines constructed.

Kubernetes / Docker Swarm / compose healthchecks typically hit /healthz;
load balancers and CI smoke checks hit /readyz to confirm the stack is
actually wired up (not just running).

C6 adds the `pii` sub-check: `/readyz` returns 503 until
`init_pii()` has completed on startup. That guard avoids a race where
the first webhook could arrive while spaCy is still loading and blow
the 2 s SLA.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Response, status

from app.db import get_pool
from app.logging import get_logger
from app.pii import is_ready as pii_is_ready

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
    """Pool SELECTs 1 AND Presidio engines are built. 503 otherwise."""
    # DB probe.
    try:
        async with pool.acquire() as conn:
            value = await conn.fetchval("SELECT 1")
            if value != 1:
                raise RuntimeError(f"SELECT 1 returned {value!r}")
        db_status = "ok"
    except Exception as exc:  # noqa: BLE001 — intentionally broad on readiness
        log.warning("readyz.db_unreachable", error=str(exc))
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "db": "unreachable", "pii": "unknown"}

    # PII probe — cheap boolean; no need for try/except.
    pii_status = "ok" if pii_is_ready() else "initializing"
    if pii_status != "ok":
        log.info("readyz.pii_not_ready")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "db": db_status, "pii": pii_status}

    return {"status": "ok", "db": db_status, "pii": pii_status}
