"""PII redaction tests (CR-10).

Exercises `app.pii.redact` directly — no FastAPI, no DB. Tests that:

  * Each of the four default entity types (PHONE_NUMBER, US_SSN,
    EMAIL_ADDRESS, CREDIT_CARD) is detected and replaced with its
    deterministic `<TYPE_REDACTED>` token.
  * The raw digit sequence for SSN / credit card no longer appears
    anywhere in the output (defence-in-depth against a Presidio bug
    that might leave a partial substring).
  * Clean text (no PII) flows through unchanged with
    `entities_removed=0`.
  * The `entity_counts` dict aggregates correctly when the same
    entity type appears multiple times.

Invocation:
    docker compose run --rm api pytest tests/test_redaction.py -v
"""
from __future__ import annotations

import pytest

from app.pii import RedactionResult, redact, reset_for_tests


@pytest.fixture(autouse=True)
def _reset_pii_singletons() -> None:
    """Clear engine cache so each test starts from a known state.

    `redact()` lazy-inits on first call, so this really just ensures a
    prior test's env override doesn't bleed through.
    """
    reset_for_tests()


def test_redact_us_ssn() -> None:
    # Presidio's UsSsnRecognizer.invalidate_result rejects "sample" SSNs
    # (123456789, 000-xx, 666-xx, etc.). Use a plausible-shaped value.
    result = redact("My SSN is 521-45-6789.")
    assert isinstance(result, RedactionResult)
    assert "<US_SSN_REDACTED>" in result.text
    assert "521-45-6789" not in result.text
    assert result.entities_removed >= 1
    assert result.entity_counts.get("US_SSN", 0) >= 1


def test_redact_email() -> None:
    result = redact("Reach me at jane.doe@example.com for questions.")
    assert "<EMAIL_ADDRESS_REDACTED>" in result.text
    assert "jane.doe@example.com" not in result.text
    assert result.entity_counts.get("EMAIL_ADDRESS", 0) >= 1


def test_redact_credit_card() -> None:
    result = redact("Card number 4111 1111 1111 1111 expires next year.")
    assert "<CREDIT_CARD_REDACTED>" in result.text
    assert "4111 1111 1111 1111" not in result.text
    # Also confirm no partial digit group leaked through.
    assert "4111" not in result.text


def test_redact_phone_number() -> None:
    result = redact("Call me at +1-415-555-1234 tomorrow afternoon.")
    assert "<PHONE_NUMBER_REDACTED>" in result.text
    assert "415-555-1234" not in result.text


def test_redact_clean_text_unchanged() -> None:
    clean = "The weather is nice today."
    result = redact(clean)
    assert result.text == clean
    assert result.entities_removed == 0
    assert result.entity_counts == {}


def test_redact_empty_text() -> None:
    result = redact("")
    assert result.text == ""
    assert result.entities_removed == 0
    assert result.entity_counts == {}


def test_redact_multiple_entity_types_aggregate() -> None:
    """All four entities in one string — counts aggregate correctly."""
    text = (
        "Hi, my SSN is 521-45-6789 and 492-65-4321. "
        "Email me at user@example.com. "
        "Card 4111 1111 1111 1111."
    )
    result = redact(text)
    assert result.entity_counts.get("US_SSN", 0) == 2
    assert result.entity_counts.get("EMAIL_ADDRESS", 0) >= 1
    assert result.entity_counts.get("CREDIT_CARD", 0) >= 1
    # Raw sensitive strings are gone.
    assert "521-45-6789" not in result.text
    assert "492-65-4321" not in result.text
    assert "user@example.com" not in result.text


def test_redact_honors_entity_override_kwarg() -> None:
    """Explicit `entities=` overrides the env default."""
    text = "SSN 521-45-6789 email someone@example.org"
    # Narrow the detector to SSN only.
    result = redact(text, entities=["US_SSN"])
    assert "<US_SSN_REDACTED>" in result.text
    assert "521-45-6789" not in result.text
    # Email NOT redacted because it wasn't in the allow-list.
    assert "someone@example.org" in result.text
