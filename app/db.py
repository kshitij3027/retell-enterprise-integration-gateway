"""Database pool accessors.

C1 scope is intentionally minimal: expose the pool from app.state to routes
that need a raw connection (today: just /readyz). The full `get_db()`
FastAPI dependency with `SET LOCAL app.tenant_id` and encryption_key wiring
lands in C2 — not here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from asyncpg.pool import Pool


async def get_pool(request: Request) -> "Pool":
    """Return the asyncpg pool attached to the FastAPI app state.

    Used by /readyz for a bare `SELECT 1` liveness probe. Not a full
    dependency — callers must open their own transaction if they intend
    to query tenant-scoped tables (that's what C2 wires up).
    """
    return request.app.state.db_pool  # type: ignore[no-any-return]
