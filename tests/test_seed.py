"""Seed script idempotency tests (CR-15).

`scripts.seed.main` is expected to:
  * Create both `tnt_lending_demo` and `tnt_health_demo` if they don't
    exist; reuse them (no exception) on a second run.
  * Issue exactly one API key per tenant on first run.
  * Carry the `phi_mode` flag correctly (True for health, False for
    lending).

We call `_run` (the async entry point) directly — the CLI wrapper is
just an asyncio.run(...) shim.
"""
from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from app.config import get_settings


async def _delete_seed_tenants() -> None:
    """Make the test hermetic by clearing both demo tenant rows first."""
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.admin_database_url)
    try:
        await conn.execute(
            "DELETE FROM tenants WHERE name IN "
            "('tnt_lending_demo', 'tnt_health_demo')"
        )
    finally:
        await conn.close()


async def _fetch_seed_rows() -> list[dict[str, Any]]:
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.admin_database_url)
    try:
        rows = await conn.fetch(
            "SELECT name, profile, phi_mode FROM tenants "
            "WHERE name IN ('tnt_lending_demo', 'tnt_health_demo') "
            "ORDER BY name"
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def _count_keys_for(tenant_name: str) -> int:
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.admin_database_url)
    try:
        count = await conn.fetchval(
            "SELECT count(*) FROM api_keys k "
            "JOIN tenants t ON t.id = k.tenant_id "
            "WHERE t.name = $1",
            tenant_name,
        )
        return int(count or 0)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed_creates_both_demo_tenants() -> None:
    from scripts.seed import _run

    await _delete_seed_tenants()
    await _run()
    rows = await _fetch_seed_rows()
    names = {r["name"] for r in rows}
    assert names == {"tnt_lending_demo", "tnt_health_demo"}

    lending = next(r for r in rows if r["name"] == "tnt_lending_demo")
    assert lending["profile"] == "consumer-lending"
    assert lending["phi_mode"] is False

    health = next(r for r in rows if r["name"] == "tnt_health_demo")
    assert health["profile"] == "healthcare"
    assert health["phi_mode"] is True

    # Exactly one key per tenant.
    assert await _count_keys_for("tnt_lending_demo") == 1
    assert await _count_keys_for("tnt_health_demo") == 1


@pytest.mark.asyncio
async def test_seed_is_idempotent() -> None:
    """Second run doesn't create duplicate tenants or extra keys."""
    from scripts.seed import _run

    await _delete_seed_tenants()
    await _run()
    await _run()  # second invocation
    rows = await _fetch_seed_rows()
    assert len(rows) == 2
    assert await _count_keys_for("tnt_lending_demo") == 1
    assert await _count_keys_for("tnt_health_demo") == 1
