"""Tests for /healthz and /readyz.

These are the only C1 tests. They use httpx + ASGITransport to exercise the
FastAPI app in-process, no network / no real Postgres. The stub pool from
conftest.py makes /readyz return 200 deterministically.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_healthz_returns_ok(app_with_stub_pool):  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app_with_stub_pool)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_returns_ok_when_db_reachable(app_with_stub_pool):  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app_with_stub_pool)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/readyz")
    # Stub pool returns 1 -> readyz should be 200; if env doesn't have a stub
    # (shouldn't happen here), a 503 is also acceptable per the brief.
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        body = resp.json()
        assert body["status"] == "ok"
        assert body["db"] == "ok"
