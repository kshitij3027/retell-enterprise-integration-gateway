"""API key generation, hashing, and verification (CR-6).

Two-tier design:
  * Raw keys are `reig_<token_urlsafe(32)>` — shown ONCE at issuance time
    and never persisted anywhere.
  * Only the SHA-256 hex digest is stored in api_keys.key_hash. Hash
    comparison uses hmac.compare_digest to avoid leaking timing info about
    how much of a candidate hash matched a real one.

The prefix ("reig_") is deliberately kept in both the raw key AND a separate
key_prefix column so ops tooling can grep / log "keys starting with reig_"
without ever seeing the secret bytes.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from app.config import get_settings


def hash_key(raw: str) -> str:
    """SHA-256 hex digest of the raw key. Deterministic; safe to store."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_key(raw: str, stored_hash: str) -> bool:
    """Constant-time comparison of `hash_key(raw)` against a stored hash.

    Uses `hmac.compare_digest` so an attacker cannot infer how many
    leading bytes matched by measuring response time. CR-6.
    """
    return hmac.compare_digest(hash_key(raw), stored_hash)


def generate_key(prefix: str | None = None) -> tuple[str, str]:
    """Mint a new (raw_key, stored_hash) pair.

    The raw key is `<prefix><token_urlsafe(32)>` — 32 bytes of URL-safe
    randomness (~43 chars) prefixed with the caller's configured prefix
    (defaults to settings.tenant_api_key_prefix, typically "reig_"). The
    raw key is returned once; callers MUST show it to the user and drop
    it. The hash is what the DB stores.
    """
    actual_prefix = prefix if prefix is not None else get_settings().tenant_api_key_prefix
    raw = f"{actual_prefix}{secrets.token_urlsafe(32)}"
    return raw, hash_key(raw)
