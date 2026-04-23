"""PII env-toggle tests (CR-10).

Covers the two settings that affect redaction behaviour at runtime:

  * `REIG_PII_ENTITIES`          — comma-separated entity allowlist.
                                   Setting it to `US_SSN` should redact
                                   SSNs but pass emails through unchanged.

  * `REIG_PII_REDACTION_ENABLED` — master switch. When false, `redact`
                                   returns the input verbatim + emits a
                                   WARN log so an operator can grep
                                   for accidental passthrough.

We monkeypatch the Settings singleton via `get_settings.cache_clear()`
since `Settings` is lru_cached.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.logging import configure_logging
from app.pii import redact, reset_for_tests


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """Clear the Settings cache + PII singletons before each test."""
    reset_for_tests()
    get_settings.cache_clear()
    # Make sure stdlib logging is configured so propagation to capfd works.
    configure_logging("DEBUG")
    yield
    get_settings.cache_clear()
    reset_for_tests()


def test_entities_setting_narrows_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting REIG_PII_ENTITIES=US_SSN → email passes through untouched."""
    monkeypatch.setenv("REIG_PII_ENTITIES", "US_SSN")
    get_settings.cache_clear()

    result = redact("email bob@example.org SSN 521-45-6789")

    assert "<US_SSN_REDACTED>" in result.text
    assert "521-45-6789" not in result.text
    # Email is not in the allowlist — flows through verbatim.
    assert "bob@example.org" in result.text


def test_redaction_disabled_returns_input_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """REIG_PII_REDACTION_ENABLED=false bypasses Presidio and WARNs."""
    monkeypatch.setenv("REIG_PII_REDACTION_ENABLED", "false")
    get_settings.cache_clear()

    original = "SSN 521-45-6789 email bob@example.org"
    result = redact(original)

    assert result.text == original
    assert result.entities_removed == 0
    assert result.entity_counts == {}

    # The WARN is load-bearing — must be visible so operators can grep it.
    # structlog's PrintLoggerFactory writes JSON to stdout; capsys captures
    # that. Accept either the raw event name or the JSON-rendered record.
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "pii.redact.disabled" in combined
