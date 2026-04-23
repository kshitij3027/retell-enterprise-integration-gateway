"""Shared adapter error taxonomy used by tenacity retry policies (CR-9).

Two flavours:

  * `TransientError` — retryable. 5xx responses, 429 rate-limits, and
    networking hiccups. tenacity re-tries with exponential backoff.
  * `PermanentError` — NOT retryable. 4xx that reflect caller errors
    (e.g. 400 DUPLICATE_VALUE, 403, 404 on a correctly-formed request).
    Bubbles straight through and lands in `crm_writes.status='failed'`.

`RetryError` from tenacity wraps a series of `TransientError`s after the
attempt budget is exhausted; the pipeline layer unwraps it into a
`failed` `crm_writes` row with the full RetryStatistics for forensics.
"""
from __future__ import annotations


class AdapterError(Exception):
    """Base class — never raised directly. Catch this to handle both flavours."""


class TransientError(AdapterError):
    """A retryable failure — 5xx, 429, connection reset, etc.

    tenacity is configured with `retry_if_exception_type(TransientError)`
    so this is the ONLY exception type that gets retried.
    """


class PermanentError(AdapterError):
    """A non-retryable failure — bad payload, auth error, record constraint.

    Wrapping 4xx responses (other than 401 `INVALID_SESSION_ID`, which we
    recover from in-place with a token refresh) in this class makes them
    bubble out of tenacity on the first attempt rather than chewing
    through the retry budget.
    """
