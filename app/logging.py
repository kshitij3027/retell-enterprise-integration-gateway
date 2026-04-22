"""Structured logging configuration.

structlog emits JSON to stdout. Every log line ends up shaped like:
    {"timestamp": "...", "level": "info", "event": "...", "trace_id": "..."}
which lets Jaeger / Loki / Datadog correlate logs with spans via `trace_id`.

Call `configure_logging(level)` exactly once at app startup.
"""
from __future__ import annotations

import logging
import sys
from typing import cast

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Wire stdlib logging -> structlog -> JSON on stdout.

    Safe to call multiple times; structlog.configure is idempotent.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # stdlib: route everything to stdout at the requested level
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Prefer module-level loggers: `log = get_logger(__name__)`."""
    # structlog.get_logger is untyped upstream (returns Any). Cast to our
    # declared return type so strict mypy sees a concrete BoundLogger.
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
