"""Retell webhook signature verification (CR-2).

Retell signs each webhook body with HMAC-SHA256 over
`api_key -> (body || str(timestamp_ms))`, hex-encoded, and ships the result
as a header `x-retell-signature: v={timestamp_ms},d={hex_digest}`.

The official SDK helper lives at `retell.lib.webhook_auth.verify` — it:
  * parses the `v=<ts>,d=<digest>` header,
  * rejects if `abs(now_ms - ts_ms) > 300_000`,
  * recomputes HMAC-SHA256(api_key, body + str(ts)) and compares.

Wrapping that helper in our own function gives us three things the raw SDK
call doesn't:
  1. A typed return value (`SignatureResult`) so callers can distinguish
     "no header" from "bad digest" from "timestamp skew" — needed for
     audit-log reasons.
  2. Explicit parsing of the timestamp so we can surface it for audit
     payloads and structured logs.
  3. A configurable skew window (defaulted to 300 s to match the SDK) so
     tests can crank the window down without monkeypatching.

CRITICAL: the caller MUST pass the raw, unparsed request body bytes. Any
re-serialization (even `json.loads()` + `json.dumps()`) reorders keys and
breaks HMAC. The route handler reads `await request.body()` before any
parsing and threads those exact bytes in here.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import NamedTuple

from app.logging import get_logger

log = get_logger(__name__)

# `v={digits},d={hex}` — whitespace-tolerant. Matches what Retell's SDK emits.
_SIG_HEADER_RE = re.compile(r"^\s*v=(?P<ts>\d+)\s*,\s*d=(?P<digest>[0-9a-fA-F]+)\s*$")


class SignatureResult(NamedTuple):
    """Outcome of a webhook signature check.

    Attributes:
        is_valid:   True only if header parsed, timestamp fresh, and digest matched.
        reason:     None on success; one of
                      "missing_header" | "malformed_header" |
                      "timestamp_skew" | "signature_mismatch"
                    on failure. Fed into audit_log.payload.reason.
        timestamp:  The parsed timestamp in milliseconds (Retell's native unit),
                    or None if the header couldn't be parsed.
    """

    is_valid: bool
    reason: str | None
    timestamp: int | None


def verify_retell_signature(
    raw_body: bytes,
    signature_header: str | None,
    api_key: str,
    skew_seconds: int = 300,
) -> SignatureResult:
    """Verify a Retell webhook signature against the raw body bytes.

    Args:
        raw_body: Exact request body bytes (no re-serialization).
        signature_header: Value of `x-retell-signature`, or None if absent.
        api_key: The Retell workspace API key used as the HMAC secret.
        skew_seconds: Max allowed |now - header_timestamp| in seconds.
            Retell's default is 300 s (5 minutes). We mirror that.

    Returns:
        SignatureResult whose `is_valid` is True iff header parsed, the
        timestamp is within skew, and the HMAC digest matches.

    The check order is deliberate: header-present → header-parseable →
    timestamp-fresh → digest-matches. That way the audit row's `reason`
    pinpoints where the request failed, which makes forensics tractable
    when a tenant or attacker reports "my webhooks aren't delivering".
    """
    if signature_header is None or signature_header == "":
        return SignatureResult(is_valid=False, reason="missing_header", timestamp=None)

    match = _SIG_HEADER_RE.match(signature_header)
    if match is None:
        return SignatureResult(
            is_valid=False, reason="malformed_header", timestamp=None
        )

    try:
        ts_ms = int(match.group("ts"))
    except ValueError:
        # Regex already constrains to digits, but belt-and-braces.
        return SignatureResult(
            is_valid=False, reason="malformed_header", timestamp=None
        )
    post_digest = match.group("digest")

    # Skew check — Retell works in milliseconds since epoch.
    now_ms = int(time.time() * 1000)
    skew_ms = skew_seconds * 1000
    if abs(now_ms - ts_ms) > skew_ms:
        return SignatureResult(
            is_valid=False, reason="timestamp_skew", timestamp=ts_ms
        )

    # HMAC recompute: sha256(api_key, body_bytes + ts_ms_ascii). Constant-time
    # compare to avoid leaking prefix-match length via timing side channel.
    try:
        body_text = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        # Retell sends JSON; a non-UTF-8 body is by definition bogus.
        return SignatureResult(
            is_valid=False, reason="signature_mismatch", timestamp=ts_ms
        )

    expected = hmac.new(
        api_key.encode("utf-8"),
        (body_text + str(ts_ms)).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, post_digest):
        return SignatureResult(
            is_valid=False, reason="signature_mismatch", timestamp=ts_ms
        )

    return SignatureResult(is_valid=True, reason=None, timestamp=ts_ms)
