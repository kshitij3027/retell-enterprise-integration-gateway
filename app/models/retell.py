"""Pydantic v2 models for Retell webhook payloads (C4).

Retell's webhook body shape is only loosely documented — different event
types ship slightly different sub-objects, the SDK ships its own typed
dataclasses that drift from the wire format, and new fields appear over
time without a schema-version bump. So these models are deliberately
PERMISSIVE:

  * `model_config = ConfigDict(extra="allow")` on both classes so fields
    we haven't modelled don't trigger validation errors. Unknown fields
    are preserved on the instance for audit/debug purposes.

  * `event: Literal[...] | str` — we enumerate the three events we route
    on (`call_started`, `call_ended`, `call_analyzed`) so mypy and the
    JSON schema surface the "happy path" values, but accept any other
    string too. Unknown events land in the `webhook.received.unknown`
    audit branch rather than 400-ing the caller.

  * Everything inside `call` except `call_id` is optional. Retell's
    `call_started` event, for instance, doesn't include a transcript yet,
    and many fields only appear on `call_analyzed`.

Fields we model explicitly (because downstream code reads them in C5+):
  * `call_id`       — required; the dedup + adapter external-id key.
  * `from_number`   — becomes SFDC Lead.Phone once C7 lands.
  * `to_number`     — the Retell number that was dialed; useful for
                      multi-number tenant routing in later commits.
  * `agent_id`      — which Retell agent handled the call; indexed for
                      per-agent analytics in the roadmap.
  * `transcript`    — redacted by Presidio in C6 before it flows into
                      SFDC Lead.Description.

`RetellCall` carries Arbitrary additional fields via `extra="allow"`.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class RetellCall(BaseModel):
    """Nested `call` object inside a Retell webhook payload.

    All fields except `call_id` are optional — different event types
    populate different subsets. Extra (unmodelled) fields are preserved
    so we can dump the whole thing into `processed_events.raw_payload`
    for forensics without losing any data Retell sends.
    """

    model_config = ConfigDict(extra="allow")

    call_id: str
    from_number: str | None = None
    to_number: str | None = None
    agent_id: str | None = None
    transcript: str | None = None


class RetellWebhookPayload(BaseModel):
    """Top-level Retell webhook envelope.

    The `event` field drives the router in `app/routes/webhooks.py`.
    It is typed as `Literal[...] | str` so known events flow through a
    typed path while unknown events fall through to the "unknown" audit
    branch without raising a 500.
    """

    model_config = ConfigDict(extra="allow")

    event: Literal["call_started", "call_ended", "call_analyzed"] | str
    call: RetellCall
