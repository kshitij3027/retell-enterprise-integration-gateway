"""PII redaction via Microsoft Presidio (CR-10).

Presidio is a two-stage pipeline:
    AnalyzerEngine   — detects PII entities (SSN, email, phone, credit card,
                       etc.) via regex + spaCy-based NER recognizers.
    AnonymizerEngine — applies an OperatorConfig per entity to rewrite the
                       detected spans. We use `replace` with a deterministic
                       token of the form `<{ENTITY_TYPE}_REDACTED>` so a
                       reviewer can eyeball the output and confirm each
                       sensitive span is gone.

Why singletons: the AnalyzerEngine constructor loads a spaCy model into
memory — somewhere between 15 MB (en_core_web_sm) and 560 MB (en_core_web_lg)
depending on which variant is installed. We MUST NOT build the engine on
every call; instead we lazy-init once at app startup (see
`app/main.py`'s lifespan) and reuse the singleton for every request.

Why en_core_web_sm in CI vs en_core_web_lg in production:
    The four entity types we redact by default
    (PHONE_NUMBER, US_SSN, EMAIL_ADDRESS, CREDIT_CARD) all have
    **pattern-based** recognizers — they don't rely on the NER model's
    accuracy to fire. The NER-backed recognizers (PERSON, LOCATION,
    ORGANIZATION) are not in our default `REIG_PII_ENTITIES` list.
    So the smaller model is functionally equivalent for our purposes
    and keeps CI install time bounded to seconds instead of minutes.
    Production Dockerfile can still pre-bake `en_core_web_lg` if a
    tenant enables PERSON/LOCATION redaction.
"""
from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.logging import get_logger

if TYPE_CHECKING:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine

log = get_logger(__name__)


# Module-level singletons. Populated by `init_pii()` on startup; None until
# then. Every call site that touches these must go through `redact()` so
# the "not initialised" code path raises a clear error instead of an
# unhelpful AttributeError on NoneType.
_analyzer: AnalyzerEngine | None = None
_anonymizer: AnonymizerEngine | None = None

# The four default entity types are all pattern-based — they work fine
# under en_core_web_sm without any NER accuracy hit.
DEFAULT_ENTITIES: tuple[str, ...] = (
    "PHONE_NUMBER",
    "US_SSN",
    "EMAIL_ADDRESS",
    "CREDIT_CARD",
)


@dataclass(frozen=True)
class RedactionResult:
    """Outcome of a `redact(...)` call.

    Attributes:
        text:             The anonymised string (or the input verbatim if
                          redaction is disabled via env).
        entities_removed: Total number of spans replaced.
        entity_counts:    Per-entity count, e.g. `{"US_SSN": 2, "EMAIL_ADDRESS": 1}`.
    """

    text: str
    entities_removed: int
    entity_counts: dict[str, int] = field(default_factory=dict)


def _build_analyzer() -> AnalyzerEngine:
    """Construct the AnalyzerEngine using the configured spaCy model.

    Reads `REIG_PII_SPACY_MODEL` (default `en_core_web_sm`) so CI can run
    against a smaller model without touching production's `en_core_web_lg`.
    If the model isn't installed on the host, Presidio raises loudly at
    construction time — that's by design: we want an explicit startup
    failure rather than silent "no redactions ever happen".
    """
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    model_name = os.environ.get("REIG_PII_SPACY_MODEL", "en_core_web_sm")
    configuration = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": model_name}],
    }
    provider = NlpEngineProvider(nlp_configuration=configuration)
    nlp_engine = provider.create_engine()
    return AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])


def init_pii() -> None:
    """Build the Analyzer + Anonymizer singletons.

    Called from `app.main`'s lifespan startup hook. Idempotent — a second
    call is a no-op so tests can force initialisation without worrying
    about double-build side effects.

    We log the time this takes because it's the single slowest thing in
    startup (spaCy model load + Presidio recognizer registry build).
    """
    global _analyzer, _anonymizer
    if _analyzer is not None and _anonymizer is not None:
        return

    import time

    from presidio_anonymizer import AnonymizerEngine

    log.info("pii.init.begin")
    t0 = time.perf_counter()
    _analyzer = _build_analyzer()
    # AnonymizerEngine's __init__ isn't fully typed upstream; cast via ignore.
    _anonymizer = AnonymizerEngine()  # type: ignore[no-untyped-call]
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log.info("pii.init.ready", elapsed_ms=round(elapsed_ms, 1))


def is_ready() -> bool:
    """True iff `init_pii()` has built the engines. Used by /readyz."""
    return _analyzer is not None and _anonymizer is not None


def reset_for_tests() -> None:
    """Clear the singletons. Test-only — production code must not call this."""
    global _analyzer, _anonymizer
    _analyzer = None
    _anonymizer = None


def redact(
    text: str,
    entities: Iterable[str] | None = None,
) -> RedactionResult:
    """Redact PII entities in `text`, replacing each span with `<TYPE_REDACTED>`.

    Args:
        text:     Input string. Empty string returns an empty RedactionResult.
        entities: Entity types to detect. None => the configured
                  `REIG_PII_ENTITIES` setting (comma-separated) or the
                  default four-entity tuple.

    Returns:
        RedactionResult with the anonymised text, the total span count,
        and a per-entity breakdown. The anonymised text is safe to persist
        or forward downstream; the ONLY copy of the raw original ever
        leaves the scope of `redact()` via its caller's choice.

    Env toggles:
        REIG_PII_REDACTION_ENABLED=false → returns the input unchanged,
        `entities_removed=0`, and logs a WARN once per call. The WARN is
        load-bearing — accidentally shipping unredacted data is a security
        incident, so the log trail MUST make "redaction was off" visible.
    """
    from app.config import get_settings

    settings = get_settings()

    if not text:
        return RedactionResult(text="", entities_removed=0, entity_counts={})

    if not settings.pii_redaction_enabled:
        log.warning(
            "pii.redact.disabled",
            text_length=len(text),
            reason="REIG_PII_REDACTION_ENABLED=false",
        )
        return RedactionResult(text=text, entities_removed=0, entity_counts={})

    # Ensure singletons exist. Production path goes through init_pii in the
    # app lifespan; tests that call redact() directly without a lifespan
    # still work because we initialise on demand.
    if _analyzer is None or _anonymizer is None:
        init_pii()
    assert _analyzer is not None and _anonymizer is not None

    # Resolve the entity list. Caller override wins; otherwise the env
    # setting; otherwise the default four-entity tuple.
    if entities is None:
        raw_cfg = settings.pii_entities.strip()
        if raw_cfg:
            entity_list = [e.strip() for e in raw_cfg.split(",") if e.strip()]
        else:
            entity_list = list(DEFAULT_ENTITIES)
    else:
        entity_list = list(entities)

    analyzer_results = _analyzer.analyze(
        text=text,
        entities=entity_list,
        language="en",
    )

    # Build one OperatorConfig per entity type — replace the span with a
    # deterministic placeholder. Presidio will fall back to the "default"
    # operator for any entity it detects that isn't in `operators`, but
    # we've already narrowed the analyzer to `entity_list` so there
    # should be no such spans.
    from presidio_anonymizer.entities import OperatorConfig

    operators = {
        ent: OperatorConfig("replace", {"new_value": f"<{ent}_REDACTED>"})
        for ent in entity_list
    }

    # Presidio ships two RecognizerResult classes with identical shape but
    # different import paths; mypy can't unify them. The runtime list is
    # interchangeable — cast away the declared-type mismatch.
    anon_result = _anonymizer.anonymize(
        text=text,
        analyzer_results=analyzer_results,  # type: ignore[arg-type]
        operators=operators,
    )

    counts: dict[str, int] = dict(
        Counter(r.entity_type for r in analyzer_results)
    )
    log.debug(
        "pii.redact.ok",
        entities_removed=len(analyzer_results),
        entity_counts=counts,
        input_length=len(text),
        output_length=len(anon_result.text),
    )
    return RedactionResult(
        text=anon_result.text,
        entities_removed=len(analyzer_results),
        entity_counts=counts,
    )
