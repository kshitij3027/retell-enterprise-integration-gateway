"""Append-only audit log enforcement tests (SC-9 / CR-14).

`audit_log` has UPDATE + DELETE revoked from the `reig_app` role. A
compromised app-tier process that tries to rewrite history will hit
`InsufficientPrivilege` at the SQL layer. Same treatment for
`processed_events` — replays re-read it, and we rely on the INSERT-ON-
CONFLICT-DO-NOTHING semantics to stay correct under load, so tampering
with existing rows must not be possible.
"""
from __future__ import annotations

import asyncpg
import pytest

from app.config import get_settings


@pytest.mark.asyncio
async def test_audit_log_update_denied() -> None:
    """UPDATE as reig_app must fail with InsufficientPrivilege."""
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.database_url)
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', "
                    "'00000000-0000-0000-0000-000000000000', true)"
                )
                await conn.execute("UPDATE audit_log SET event_type = 'xxx'")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_audit_log_delete_denied() -> None:
    """DELETE as reig_app must fail with InsufficientPrivilege."""
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.database_url)
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', "
                    "'00000000-0000-0000-0000-000000000000', true)"
                )
                await conn.execute("DELETE FROM audit_log")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_processed_events_update_denied() -> None:
    """processed_events rows must be append-only too — ON CONFLICT integrity."""
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.database_url)
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', "
                    "'00000000-0000-0000-0000-000000000000', true)"
                )
                await conn.execute(
                    "UPDATE processed_events SET event_type = 'x'"
                )
    finally:
        await conn.close()
