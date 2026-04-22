"""Tests for `app.auth` — hash determinism, compare_digest, prefix hygiene.

These are unit tests — no Postgres required. Run inside Docker:
    docker compose run --rm api pytest tests/test_auth.py -v
"""
from __future__ import annotations

import hashlib
import hmac

import pytest

from app import auth


def test_hash_key_is_deterministic() -> None:
    """Same input -> same hash. Different inputs -> different hashes."""
    h1 = auth.hash_key("reig_abc123")
    h2 = auth.hash_key("reig_abc123")
    h3 = auth.hash_key("reig_abc124")
    assert h1 == h2
    assert h1 != h3


def test_hash_key_returns_sha256_hex() -> None:
    """hash_key must be a plain SHA-256 hex digest (64 lowercase hex chars)."""
    raw = "reig_xyz"
    expected = hashlib.sha256(raw.encode()).hexdigest()
    got = auth.hash_key(raw)
    assert got == expected
    assert len(got) == 64
    assert all(c in "0123456789abcdef" for c in got)


def test_verify_key_true_on_match() -> None:
    raw = "reig_somekey_aaa"
    assert auth.verify_key(raw, auth.hash_key(raw)) is True


def test_verify_key_false_on_mismatch() -> None:
    assert auth.verify_key("reig_a", auth.hash_key("reig_b")) is False


def test_verify_key_uses_hmac_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: verify_key MUST route through hmac.compare_digest.

    Monkeypatch hmac.compare_digest to a sentinel that raises — if verify_key
    bypasses it (e.g. falls back to ==), the test wouldn't raise and we'd
    be leaking timing info. This test therefore proves the call-through.
    """
    called: dict[str, bool] = {"was_called": False}

    def _spy(a: object, b: object) -> bool:
        called["was_called"] = True
        raise RuntimeError("compare_digest path reached")

    monkeypatch.setattr(hmac, "compare_digest", _spy)
    # The module binds `hmac` at import time, so also patch the name in
    # the auth module itself for maximum safety.
    monkeypatch.setattr(auth.hmac, "compare_digest", _spy)

    with pytest.raises(RuntimeError, match="compare_digest path reached"):
        auth.verify_key("x", "y")
    assert called["was_called"] is True


def test_generate_key_has_prefix_and_hash_roundtrip() -> None:
    raw, stored = auth.generate_key(prefix="reig_")
    assert raw.startswith("reig_")
    # token_urlsafe(32) → ~43 chars base64url, so total >= 48 chars.
    assert len(raw) > 40
    # Stored hash must match hash_key(raw).
    assert stored == auth.hash_key(raw)
    # And verify_key should accept it.
    assert auth.verify_key(raw, stored) is True


def test_generate_key_is_unique() -> None:
    """Two successive generate_key calls must yield distinct keys."""
    raw1, _ = auth.generate_key()
    raw2, _ = auth.generate_key()
    assert raw1 != raw2


def test_generate_key_custom_prefix() -> None:
    raw, _ = auth.generate_key(prefix="custom_")
    assert raw.startswith("custom_")
