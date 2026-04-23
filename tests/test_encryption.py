"""pgcrypto helper tests (CR-7).

`encrypt_refresh_token` + `decrypt_refresh_token` are thin wrappers around
`pgp_sym_encrypt` / `pgp_sym_decrypt` that read the key from
`current_setting('app.encryption_key')`. These tests assert:

  * encrypt → bytea ciphertext that is NOT equal to the plaintext UTF-8.
  * decrypt(encrypt(x)) == x (round-trip).
  * Attempting to call encrypt WITHOUT `app.encryption_key` set raises.

Docker-only runbook per the plan:
    docker compose run --rm api pytest tests/test_encryption.py -v
"""
from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from app.config import get_settings


@pytest.mark.asyncio
async def test_encrypt_round_trip() -> None:
    """encrypt_refresh_token → bytea, decrypt_refresh_token → plaintext."""
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.admin_database_url)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.encryption_key', $1, true)",
                settings.encryption_key,
            )
            plain = "sfdc_refresh_token_abc123"
            ciphertext: Any = await conn.fetchval(
                "SELECT encrypt_refresh_token($1)", plain
            )
            # bytea comes back as bytes; MUST NOT match the plaintext.
            assert isinstance(ciphertext, bytes | bytearray | memoryview)
            assert bytes(ciphertext) != plain.encode("utf-8")
            # Round-trip must recover the original.
            recovered: str = await conn.fetchval(
                "SELECT decrypt_refresh_token($1)", ciphertext
            )
            assert recovered == plain
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_encrypt_fails_without_app_key() -> None:
    """Missing `app.encryption_key` causes pgcrypto to raise."""
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.admin_database_url)
    try:
        # NO set_config for app.encryption_key — the helper must fail.
        with pytest.raises(Exception) as exc_info:
            await conn.fetchval("SELECT encrypt_refresh_token('x')")
        # Accept either a pg_exception or generic — message mentions app.encryption_key.
        assert "app.encryption_key" in str(exc_info.value) or "unrecognized" in str(exc_info.value).lower()
    finally:
        await conn.close()
