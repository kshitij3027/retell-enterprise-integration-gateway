"""Retell webhook signing helpers for tests.

Mirrors the algorithm used by `retell.lib.webhook_auth.symmetric["sign"]`
so tests can produce `x-retell-signature` headers accepted both by our
own `verify_retell_signature` and by the upstream SDK's `verify`.

Algorithm (confirmed by reading retell-sdk 4.26.0 source):
    digest = HMAC-SHA256(key=api_key,
                         msg=body_utf8 + str(timestamp_ms)).hexdigest()
    header = f"v={timestamp_ms},d={digest}"

Timestamp is **milliseconds** since epoch (Retell's native unit), NOT
seconds.

This module has no production imports — it exists only under tests/.
"""
from __future__ import annotations

import hashlib
import hmac
import time


def sign_payload(
    raw_body: bytes,
    api_key: str,
    ts: int | None = None,
) -> str:
    """Produce a valid `x-retell-signature` header for `raw_body`.

    Args:
        raw_body: Exact bytes the test will POST. Do NOT re-encode JSON
            after calling this — any whitespace or key-order change
            will invalidate the HMAC.
        api_key: HMAC secret (test fixture value; never a real key).
        ts: Optional override timestamp in **milliseconds**. Defaults to
            "now". Pass an old value to produce a stale-skew signature
            for 401 tests.

    Returns:
        Header string formatted `v={ts_ms},d={hex_digest}`.
    """
    if ts is None:
        ts = int(time.time() * 1000)
    body_text = raw_body.decode("utf-8")
    digest = hmac.new(
        api_key.encode("utf-8"),
        (body_text + str(ts)).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"v={ts},d={digest}"
